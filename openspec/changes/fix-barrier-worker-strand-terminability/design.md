## Context

Issue #1645 owns four explicit Barrier strand sites: two in `tests/test_gateway_reconcile.py` and two in `tests/test_scheduler_file_provider_refresh.py`. At the first gateway site, repository/session construction happens before an unbounded barrier and the main thread uses bare joins; at the second, repository construction precedes an unbounded barrier inside `ThreadPoolExecutor`, whose context exit waits for every worker. The scheduler sites already capture errors and use bounded joins, but their barrier waits are unbounded and their peers are non-daemon, so an assertion can fire while interpreter shutdown still hangs.

Fixture level: **expanded**. Repair intensity: **high** because a failed harness can consume the entire pytest/CI run and the same whole-run invariant spans two large test modules. No upstream suggested level or minimal mergeable slice was supplied.

## Goals / Non-Goals

**Goals:**

- Make all four issue-owned Barrier protocols bounded and guarantee every started peer exits before a test returns.
- Preserve worker exception identity and report it before result/state assertions.
- Preserve the original race windows and substantive concurrency oracles.
- Prove whole-process terminability with bounded external evidence rather than waiting for a hang.

**Non-Goals:**

- Do not change production code, DB semantics, file publication, scheduler behavior, dependencies, selector rules, CI timeouts, `pyproject.toml`, or global warning policy.
- Do not absorb #1646's repository-wide `filterwarnings`/pytest-timeout decision or #1648's production-state polling diagnostics.
- Do not claim every Barrier in the evolving repository is governed; this change closes the four sites explicitly added to #1645. Other current sites are controls or separately routed when their concrete shape warrants it.

## Decisions

### D1 — Specify outcomes, not one shared helper shape

Each Barrier SHALL carry an intrinsic timeout or every wait SHALL carry the same explicit bounded timeout. A pre-arrival failure SHALL break/release peers within that bound, every worker result/exception SHALL be observed by the parent, and no started peer may remain alive when the test returns. The two modules may use their idiomatic local mechanism: explicit threads with error capture/join assertions, or futures whose `result()` re-raises. A cross-test-module helper is rejected because it creates a new import/selector ownership surface merely to share a few synchronization lines.

### D2 — Barrier timeout is the whole-run release primitive

A bounded main-thread join alone does not satisfy the contract: if a participant never arrives, its non-daemon peers remain inside `Barrier.wait()` and `threading._shutdown()` joins them indefinitely. Giving the Barrier protocol a timeout converts the missing participant into `BrokenBarrierError`, releasing all waiters. Threads remain non-daemon by default; unlike the #1633 spin-wait helper, these workers are finite once the barrier breaks and must be fully joined rather than abandoned.

### D3 — Preserve and order diagnostic truth

Explicit-thread harnesses capture `BaseException` and assert the error collection plus thread liveness before examining race results. Executor harnesses consume every future/result so constructor, `BrokenBarrierError`, and code-under-test exceptions propagate to pytest. Failure-injection tests must distinguish a pre-arrival injected exception from peer `BrokenBarrierError`; downstream missing-result assertions cannot mask either.

### D4 — Prove the real harness, not a copied toy

Tests SHALL exercise shared/local seams actually used by the four production tests or construct source mutations against those exact sites. A bounded subprocess injects one pre-arrival failure into a real harness family and must terminate. Removing the Barrier timeout (or restoring the unbounded/non-daemon strand shape) must hit the parent's external timeout after the assertion/failure path begins. Temporary probes run in an isolated copy and kill/reap the process group on timeout.

### D5 — Preserve race oracle strength

The 8-way SQL idempotency race, 2-way file submit collision, 20-way process-lock serialization, and 40-way receipt retention race keep their participant counts, real repository/provider calls, winner/loser/state/history assertions and concurrency release point. The repair changes only synchronization/error propagation/lifetime mechanics.

## Risk Packs Considered

- Public API / CLI / script entry: not selected — test-only private harnesses.
- Config / project setup: not selected — no global pytest/CI configuration change.
- File IO / path safety / overwrite: not selected — existing temporary-file operations stay as unchanged oracle inputs.
- Schema / columns / units / field names: not selected — no schema/data contract change.
- Auth / permissions / secrets: not selected — no auth boundary.
- Concurrency / shared state / ordering: selected — Barrier arrival, break propagation, peer lifetime and exception ordering are the change.
- Resource limits / large input / discovery: selected narrowly — every barrier/join/subprocess wait is bounded; no large input/discovery surface.
- Legacy compatibility / examples: selected — all four race oracles and participant counts remain unchanged.
- Error handling / rollback / partial outputs: selected — pre-arrival and peer errors must be attributed, not swallowed or misreported.
- Release / packaging / dependency compatibility: not selected — no dependency/package change.
- Documentation / migration notes: not selected — only test contract/OpenSpec documentation changes.
- All NHMS domain packs: not selected — no geospatial, hydro-met, SHUD numerical, PostGIS/Timescale production semantics, Slurm lifecycle, external provider, run-manifest/QC, or display identity behavior changes. SQLite/file-backed test fixtures remain local oracles, not domain behavior changes.

## Invariant Matrix

- Governing invariant: every issue-owned Barrier harness SHALL turn a missing/failed participant into a bounded, attributed test failure and SHALL leave no live worker that can strand interpreter shutdown, while preserving its original race oracle.
- Source of truth: the four named test functions and their Barrier participant counts/results.
- Producers: explicit worker functions and `ThreadPoolExecutor` callables in the two target modules.
- Validators/preflight: Barrier timeout, worker/future exception propagation, bounded join/result consumption, liveness assertions, failure-injection tests and subprocess mutant.
- Storage/cache/query: temporary SQLite and file journal/provider artifacts under pytest `tmp_path`; unchanged production storage.
- Public routes/entrypoints: pytest collection/execution of the four named tests; no shipped entrypoint.
- Frontend/downstream consumers: CI targeted selector executes changed test files; no frontend consumer.
- Failure/cleanup/stale state: pre-arrival constructor failure, peer `BrokenBarrierError`, code-under-test failure, executor/context cleanup, non-daemon interpreter shutdown and process-group reap.
- Evidence/audit/readiness: focused tests, whole-file tests, Ruff, strict OpenSpec, bounded subprocess/mutants, selector probe and final-head PR CI.
- Regression rows:
  - all participants arrive -> original four concurrency outcomes and participant counts remain unchanged;
  - one participant raises before arrival -> peers leave the barrier within the bound, the injected cause plus peer break are visible, and no worker remains alive;
  - one participant never arrives / timeout leg removed -> shipped harness fails bounded; unbounded mutant externally times out and is reaped;
  - changed test files -> CI selects and executes both assertion suites rather than collect-only.

## Boundary Surface Checklist

- Shared helper roots: module-local harness seams only; no cross-module helper.
- Public entrypoints: four pytest test functions.
- Read/write surfaces: existing SQLite/file-provider tmp artifacts remain unchanged.
- Producer/consumer evidence: worker/future outcome -> parent assertion -> pytest process exit.
- Stale/lifetime boundary: every started worker/future is joined/consumed; no peer outlives the test.
- Unchanged downstream consumers: selector behavior, production code and substantive race assertions.

## Risks / Trade-offs

- [Timeout too tight creates flakes] → choose a generous hang backstop, not a performance SLA; keep short bounds only in controlled injection probes.
- [Catching `BrokenBarrierError` hides the original exception] → preserve the injected pre-arrival exception separately and assert errors before race-result data.
- [Daemon threads mask cleanup] → keep these workers non-daemon and require all peers to terminate/join; daemon-only is rejected here.
- [Mutation proof hangs the host] → run only in bounded subprocess/process group and prove cleanup.

## Migration Plan

1. Add failure-injection and terminability tests/mutants against the four harnesses.
2. Bound Barrier waits and parent observation paths without changing substantive race bodies.
3. Run focused/whole-file suites, lint/spec and final-head CI.
4. Roll back by reverting both test modules and fixture together; never remove only a timeout while retaining liveness claims.

## Open Questions

None.
