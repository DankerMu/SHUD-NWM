# Tasks: journal-durable-state-preservation

Fixture level: expanded. Repair intensity: broad-expanded. Issues: #1652, #1630, #1629. Upstream suggested level: absent. Minimal mergeable slice: user explicitly selected one PR for the three sibling durable-state boundaries.

## 1. Red-first durable-boundary tests

- [x] 1.1 Add one parameterized durable-layer hydro-run test: seed a real whole-URI or embedded-URI error message, apply each non-clearing status (`staged`, `submitted`, `running`, `failed`) without error arguments, and assert exact JSONL/latest durable preservation plus public redaction. Record the batched pre-change red output.
- [x] 1.2 Keep/add the reverse successful-state lock: `succeeded` with omitted error arguments still clears durable `error_code` and `error_message` to `None`.
- [x] 1.3 Add permanent-failure round-trip tests: a public snapshot cannot replace a richer durable path/URI message; a distinct new source message still overrides; status/error code/finished_at/event details and missing/stale/idempotent exits remain unchanged. Record a source-only mutation that restores the old public-message feedback and turns the preservation case red.
- [x] 1.4 Add a validator-backed, test-only seed for a schema-valid durable `cancelled` accepted-submit master through the existing outgoing-record/direct-row seam (`tests/test_file_orchestration_journal.py::_cancelled_cohort_master`); do not add a production writer. Reproject it with explicit complete task projections, `complete=True`, the bound master Slurm ID, matched-bound decision, a fixed finished timestamp, exit code, real `s3://` log URI, and an exact derived error code/message. Assert the master’s exact refreshed candidate projections, derived error family, timestamp, exit code, and real log URI in JSONL and its direct row; assert the public query still reports `status="cancelled"` with `log_uri="[object-uri]"`. `latest/` intentionally excludes cohort masters, so verify only that its existing per-model candidate/hydro projection is refreshed and that no master copy is introduced. Include this new-behavior case in the batched pre-change red proof. Keep parameterized reverse locks for all three derived statuses.
- [x] 1.5 Keep routing locks showing `submission_failed` and `reservation_lost` remain outside complete task projection; do not construct a projection that rewrites their accounting tuple.

## 2. Durable/public read ownership

- [x] 2.1 Change `_hydro_run_for` to return an exact copied durable row under the existing cycle read/lock discipline; keep `_public_scheduler_row` at explicit public return/query boundaries.
- [x] 2.2 Audit every `_hydro_run_for` caller (repository writers, retry service, ops repair script, scheduler tests) and record which relies on durable values; do not add a second redundant private reader.
- [x] 2.3 Remove/update the #1652 “deliberately unresolved” comments and remove `update_hydro_run_status` from `_resolved_caller_evidence`'s open-gap list.

## 3. Permanent-failure message ownership

- [x] 3.1 In `_mark_master_permanently_failed`, forward a message override only when the non-empty source message differs from the current public message; otherwise pass `None` so the typed transition preserves the durable value.
- [x] 3.2 Keep `_durable_error_message`, typed transition authority, event details, error-code selection, warning behavior, and missing/stale/idempotent outcomes unchanged; update the helper's gap-list documentation to remove #1630.

## 4. Cohort terminal-state ownership

- [x] 4.1 Name the three task-projection-derived master statuses and derive the sticky-status decision from the current persisted status plus the routed domain; preserve `permanently_failed` and `cancelled` status without admitting `submission_failed` or `reservation_lost` into projection.
- [x] 4.2 Keep attribution-family stickiness restricted to `permanently_failed`; a cancelled master continues to take current task-derived error values and all observational/task evidence.
- [x] 4.3 Update the prior D5 comments/spec language that declared cancelled status stickiness out of scope; keep derived-status and no-empty-write guards intact.

## 5. Risk-pack evidence and verification

- [x] 5.1 File IO / schema / provenance: inspect durable JSONL, direct row where applicable, and latest materialization; exact URI-bearing messages survive and no display placeholder is persisted as new truth.
- [x] 5.2 Auth/redaction / public API compatibility: public return/query rows still contain `[local-path]`/`[object-uri]`, never raw protected values; method signatures and row shapes are unchanged.
- [x] 5.3 Concurrency/order / rollback: all read-merge-write operations stay inside existing cycle locks; no extra unlocked reread, append, partial write, or changed stale/idempotent outcome.
- [x] 5.4 Legacy compatibility: schema-valid historical `cancelled` rows load and preserve status; no migration or backfill; ops repair script remains correct with the now-durable private read.
- [x] 5.5 Run `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_gateway_reconcile_*.py` (the #1809-partitioned gateway-reconcile suite).
- [x] 5.6 Run `uv run ruff check .` and `openspec validate journal-durable-state-preservation --strict --no-interactive`.

## 6. Evidence and deviation record

- [x] 6.1 Report the batched red proof against pre-change runtime source, final green commands, changed files, audited sibling surfaces, and every Invariant Matrix row.
- [x] 6.2 Report every departure from this fixture as `what / why / impact`; state `no deviations` explicitly when none.

## Risk Packs

- Public API / CLI / script entry: selected — private read semantics and ops script audited; public rows unchanged.
- Config / project setup: not selected — no configuration.
- File IO / path safety / overwrite: selected — journal/direct/latest durable state.
- Schema / columns / units / field names: selected — existing status/error semantics, no shape change.
- Auth / permissions / secrets: selected — public redaction boundary must remain closed.
- Concurrency / shared state / ordering: selected — locked read-merge-write ownership.
- Resource limits / large input / discovery: not selected — no discovery/limits change.
- Legacy compatibility / examples: selected — persisted cancelled rows and internal readers.
- Error handling / rollback / partial outputs: selected — typed transition exits and no partial writes.
- Release / packaging / dependency compatibility: not selected — no dependencies.
- Documentation / migration notes: selected — prior declared gaps are superseded; no migration.
- Run manifest / QC provenance: selected — exact durable attribution is the governing evidence.
- All other NHMS domain packs: not selected — no geospatial, forcing, numerical, DB, provider, Slurm scheduling, or display artifact identity surface.

## Non-Goals

- Repairing already-corrupted durable rows, changing redaction/strip behavior, adding a cancelled writer, changing event payload redaction, DB behavior, public schemas, or remote deployment/runtime behavior.
