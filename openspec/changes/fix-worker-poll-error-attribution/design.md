## Context

Issue #1648 routes three bounded polls of production state. Its original causal description is stale: `_LeaseHeartbeat._run` catches `Exception` from `FileSchedulerLease.renew`, converts it to `renewed=False`, and sets production state `lost=True`; therefore neither heartbeat poll is an uncaught-worker-exception path. The existing heartbeat test still has two diagnostic defects: an exception before the first successful renewal becomes an empty `heartbeat_seq` symptom, and an exception after token replacement can satisfy the expected `lost` oracle and false-green. In the supervisor test, `finalize()` catches nothing and `read()` catches only expected `TerminalStateError`, so either worker can emit a non-fatal `PytestUnhandledThreadExceptionWarning` while the parent reports an empty result symptom.

Fixture level: **expanded**. Repair intensity: **high** because concurrency, shared state, a production-closure terminal path, stale issue scope, and cause-vs-state ordering span two large test modules. No upstream suggested level or minimal mergeable slice was supplied.

## Goals / Non-Goals

**Goals:**

- Preserve production `_LeaseHeartbeat` semantics while making the shipping test distinguish `renew() -> False` from `renew()` raising.
- Surface every issue-owned unexpected worker cause before downstream `heartbeat_seq`, `lost`, `finalizer_result`, or `reader_result` assertions.
- Preserve the normal takeover, expected terminal-intent error, bounded waits/joins, lock cleanup, and final receipt publication.
- Prove the repair against the exact shipping test functions, not a copied toy worker.

**Non-Goals:**

- Do not edit `_LeaseHeartbeat`, lease/runtime behavior, terminal-state production code, receipt schema/content, or scheduler fail-closed boundaries.
- Do not make production exceptions escape the daemon thread or replace `lost` with a test error channel.
- Do not absorb #1646's repository-wide warning-as-error/pytest-timeout policy, add a dependency, or prescribe cancellation of an indefinitely blocked thread.
- Do not claim these production-state polls satisfy or violate #1633's dedicated completion-sentinel requirement; the main spec explicitly excludes them from that shape.

## Decisions

### D1 — Observe the real heartbeat call at the test boundary

The heartbeat test will wrap the exact bound `lease.renew` callable it gives to the real `_LeaseHeartbeat`, record `BaseException`, and re-raise. Production `_run` remains responsible for its existing `Exception -> lost=True` mapping. The parent checks the recorded cause after each bounded production-state poll and before the state assertion. A test-only alternate heartbeat implementation is rejected because it would not prove the shipping seam.

### D2 — Keep return-false and raise as different outcomes

The existing token replacement must still drive the real lease implementation to return `False`, after which `lost=True` is the substantive production oracle. A raised exception is recorded separately and cannot satisfy that takeover assertion. A dedicated control pins the already-correct production exception-to-`lost` mapping; this is not a production change.

### D3 — Separate expected domain results from unexpected worker failures

The supervisor finalizer and reader each get a parent-visible unexpected-error channel. `TerminalStateError` remains caught first and appended to `reader_result`, because an intent-pending error is the expected domain outcome under the held lock. Only other `BaseException` values enter the unexpected reader channel. Parent liveness and unexpected-error checks precede worker-produced result assertions.

### D4 — Cleanup precedes cause surfacing

The parent continues bounded joins and releases/closes its owned file lock before an error assertion can raise. The captured-failure path must leave both test-owned workers terminated and the descriptor unlocked/closed; cause-first diagnostics do not authorize leaking the test fixture. Arbitrary permanent blocking outside the existing bounds remains #1646 scope.

### D5 — Direct failure injection proves the shipping owners

Tracked tests invoke the two exact shipping test functions under controlled monkeypatches: first-renew raise, post-takeover raise, finalizer raise, and unexpected reader raise. Each injected callable must execute and its unique exception text must be the parent-visible failure. Before repair these rows respectively report a state symptom, false-green, or warning-plus-empty-result symptom. Normal tests prove expected return/result channels remain intact.

### D6 — Keep helpers local and selectors unchanged

The two test modules may use small local capture code; no cross-module test helper is introduced for three local call sites. CI's existing changed-test-file routing is verified, not edited.

## Risk Packs Considered

- Public API / CLI / script entry: not selected — no shipped entrypoint changes.
- Config / project setup: not selected — no pytest/CI configuration or dependency changes.
- File IO / path safety / overwrite: selected narrowly — preserve unlock/close and terminal-intent/receipt fixture cleanup on captured worker failure; no production path algorithm changes.
- Schema / columns / units / field names: not selected — no lock payload or receipt schema change.
- Auth / permissions / secrets: not selected — no trust or credential boundary.
- Concurrency / shared state / ordering: selected — daemon heartbeat observation, two test-owned workers, polling/join and cause-before-state ordering.
- Resource limits / large input / discovery: selected narrowly — preserve explicit monotonic polling and join bounds; no input/discovery change.
- Legacy compatibility / examples: selected — preserve real renewal, stolen-token takeover, expected intent-pending reader error and final publication assertions.
- Error handling / rollback / partial outputs: selected — distinguish expected state/domain outcomes from unexpected exceptions and preserve cleanup.
- Release / packaging / dependency compatibility: not selected — test-only, no dependency/package change.
- Documentation / migration notes: not selected — OpenSpec is the only contract documentation needed.
- Geospatial / CRS / basin geometry: not selected — no geospatial surface.
- Hydro-met time series / forcing windows: not selected — no forcing data or time window.
- SHUD numerical runtime / conservation / NaN: not selected — no solver/runtime computation.
- PostGIS / TimescaleDB domain behavior: not selected — no database oracle.
- Slurm production lifecycle / mock-vs-real parity: not selected — no Slurm scheduling change.
- External hydro-met providers / snapshot reproducibility: not selected — no provider boundary.
- Run manifest / QC provenance: not selected — no manifest/QC evidence.
- Published NHMS artifacts / display identity: selected narrowly — preserve terminal receipt publication/identity assertions; no live artifact behavior changes.

## Invariant Matrix

- Governing invariant: each issue-owned production-state polling test SHALL surface an unexpected cause observable at its worker boundary before asserting worker-produced state/results, while preserving expected production fail-closed and domain-error outcomes.
- Source of truth: `FileSchedulerLease.renew` return-versus-raise, `_LeaseHeartbeat.lost`, expected `TerminalStateError`, the terminal-intent path, finalizer/reader result lists, and the two named shipping test functions.
- Producers: production `_LeaseHeartbeat._run`; test-owned `finalize()` and `read()` workers.
- Validators/preflight: test-local exception capture, bounded polling/joins, liveness/error ordering, direct-call injection tests.
- Storage/cache/query: scheduler lock payload and supervisor terminal-intent/receipt files under `tmp_path`; no persistent production storage change.
- Public routes/entrypoints: pytest collection/execution of the two shipping tests and their regression tests; no production public entrypoint.
- Frontend/downstream consumers: changed-file CI selector and pytest result reporting; no frontend consumer.
- Failure/cleanup/stale state: first renewal raises, post-token-replacement renewal raises, normal stolen-token false return, finalizer unexpected failure, expected reader domain error, unexpected reader failure, thread join and owned-lock release/close.
- Evidence/audit/readiness: pre-fix direct-call probes, focused tests, both full target files, selector tests, Ruff, strict OpenSpec, final-head assertion-executing CI.
- Regression rows:
  - normal renewal then token replacement -> `heartbeat_seq` advances, the real renewal returns false after theft, `lost=True`, and no captured error exists;
  - renewal raises before the first sequence increment -> production sets `lost=True`, but the shipping test fails with the injected cause before the sequence assertion;
  - renewal raises after token replacement -> production sets `lost=True`, but the shipping test rejects the exception rather than accepting it as takeover success;
  - direct heartbeat exception control -> unchanged production `_run` maps `Exception` to `lost=True` without requiring exception propagation;
  - held lock with normal finalizer/reader -> finalizer returns `False`, expected `TerminalStateError` populates `reader_result`, and final publication still succeeds;
  - finalizer or reader raises unexpectedly -> the parent names the injected cause before empty/missing result assertions, joins both workers, and releases/closes the lock;
  - either target test file changes -> selector coverage executes the owning assertion suite rather than collect-only.

## Boundary Surface Checklist

- Shared helper roots: none; module-local test observation only.
- Public entrypoints: two shipping pytest functions plus tracked direct-call injection tests.
- Read surfaces: scheduler lock payload, terminal intent and terminal receipt under `tmp_path`; production readers unchanged.
- Write/delete/overwrite surfaces: real lease renewal/token replacement and terminal receipt finalization remain unchanged oracles.
- Producer/consumer evidence: worker call outcome -> test-local capture or expected result -> parent assertion -> pytest report.
- Stale-state/idempotency boundary: stolen lease token must remain distinguishable from an exception; stale terminal identity/finalization behavior remains unchanged.
- Unchanged downstream consumers: scheduler runtime's `heartbeat.lost` fail-closed branch, terminal-state production code, CI selector, node-27 deployment.

## Risks / Trade-offs

- [A wrapper accidentally replaces the real renewal oracle] -> capture and call the exact bound method; assert injection call counts and retain the normal token-takeover test.
- [Expected `TerminalStateError` is mislabeled unexpected] -> catch the expected type before `BaseException` and keep the normal reader-result assertion.
- [Cause-first assertion leaks the held lock] -> perform bounded joins and lock release/close before surfacing captured failures.
- [A tight timer adds flakes] -> retain existing generous production-state bounds; controlled injections may use deterministic call points, not timing assumptions.
- [Local repair is mistaken for global policy] -> do not edit `pyproject.toml`, warning filters, timeout plugins, CI workflows, or #1646-owned surfaces.

## Migration Plan

1. Add tracked direct-call failure injections and record red behavior against pre-change shipping tests.
2. Add local observation/error channels without editing production modules or substantive state oracles.
3. Run focused and whole-file tests, selector coverage, lint/spec validation and same-SHA CI.
4. Roll back by reverting both test modules and this fixture together; production needs no migration.

## Open Questions

None.
