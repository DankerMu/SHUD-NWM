# Design

Fixture level: expanded · Project profile: NHMS · repair intensity: standard.
Line cites against `origin/master` `4f3fd89a`; symbol names are authoritative.

## Change surface

- `services/orchestrator/file_orchestration_journal.py`: `_cycle_rows_by_model_unlocked`
  (`:5848-5925`) and its six in-module callers (`:2397 :3690 :4052 :4215 :4903`
  direct, `:9951` the `_materialize_latest_unlocked` routing arm); `_next_sequence` (`:9603-9605`);
  `_cycle_scope_from_file_run_id` (`:12647-12677`) and the `_ANALYSIS_RUN_ID_RE`
  import (`:129-131`); the inline code-clearing set at `:2485`.
- `services/orchestrator/reservation.py`: `:45-46` comment, `:67-76` function.
- `services/orchestrator/scheduler_state_types.py`: `:29-35` (single definitions
  and comments), new `HYDRO_RUN_CODE_CLEARING_STATUSES`.
- `services/orchestrator/chain.py:207-208`, `chain_repository.py:18-19`,
  `scheduler_state_failure.py:148-149` and `:321`: aliases / shared name.
- Tests: `tests/test_file_orchestration_journal.py` (five `_next_sequence`
  sites, new #1762 pins), `tests/test_gateway_reconcile_file_cohort_identity.py:766-771`
  and `tests/test_gateway_reconcile_comment_accounting.py:147` (stub / kwarg),
  `tests/test_retry.py:164-176` (docstring only), new
  `tests/test_hydro_status_set_parity.py`.

## Must preserve

- Every `_cycle_rows_by_model_unlocked` caller's rows are byte-identical: the
  function today never includes direct jobs and never stores (every caller
  passes `False`), so dropping the flag preserves the only executed path.
  `_materialize_latest_unlocked(include_direct_jobs=True)` still routes to
  `_cycle_rows`; `False` still routes to the batch reducer. The materialiser
  has nine callers: the three that pass `include_direct_jobs=False`
  (`:3771 :4111 :4323`) keep it verbatim — those kwargs belong to the
  materialiser, not the reducer, and dropping them would route the
  cohort/accepted-submit write paths through the fingerprinted `_cycle_rows`
  arm — and the six that take the default `True`
  (`:5202 :9041 :9181 :9227 :9427 :9998`) stay as they are; adding `False`
  to them would drop direct `pipeline_job` rows from the latest views they
  build (round-1 cand-03).
- `_next_sequence_unlocked`'s sequence floor, its `journal_read_lane`
  framing, and its fail-loud containment behavior (E2/E4/E5 tests keep their
  assertions verbatim).
- `_cycle_scope_from_file_run_id` on forecast and cohort shapes (`:14924-14925`);
  `None` on any other shape and the D4 fall-open consequence
  (`:15142`); `parse_run_cycle("analysis_...")` returns the start cycle
  (`tests/test_run_identity.py:27-32`), so `retention.py:276` keeps recycling
  analysis workspaces.
- Reservation module exports with callers: `IDEMPOTENCY_KEY_RE`,
  `validate_idempotency_key`, `slurm_comment_for`, `idempotency_key_from_comment`,
  `reserve_candidate`.
- Hydro status set **membership** is unchanged on every site:
  `DURABLE_HYDRO_SUCCESS_STATUSES == COMPLETED_HYDRO_STATUSES == {"succeeded","parsed","published","complete"}`;
  `ACTIVE_HYDRO_STATUSES` keeps its per-module membership (types with
  `"pending"`, chain/chain_repository without); the code-clearing set keeps
  its six members. `has_completed_pipeline` (`chain_repository.py:98-110`),
  `scheduler_state_decision.py:228`, journal `:1282` / `:1363`,
  `chain_forecast_trigger.py:387`, `_durable_shud_output_exists`
  (`scheduler_state_failure.py:145-150`) and the journal write at `:2485`
  decide verbatim-identically on every fixture.
- Name-import surfaces: `scheduler_state_compat.py:12,:20`, `scheduler.py:74,:82`,
  `scheduler_state.py:157,:165`, `scheduler_state_decision.py:45-47`,
  `scheduler_state_manual_retry.py:34`, journal `:71-72` (imports
  `ACTIVE_HYDRO_STATUSES` / `COMPLETED_HYDRO_STATUSES` from `chain_repository`),
  `chain_forecast_trigger._completed_hydro_statuses()` (`getattr(_chain, ...)`
  monkeypatch seam — the name must stay on `chain`). The private-name
  inventory tables at `tests/test_production_scheduler.py:27706` and `:27769`
  require `_HYDRO_RUN_CODE_CLEARING_STATUSES` to remain an attribute of
  `scheduler_state_failure` consumed by `_downstream_recorded_error_code`,
  and `:27962` pins its **top-level type**: the shared
  `scheduler_state_types.HYDRO_RUN_CODE_CLEARING_STATUSES` SHALL be a
  `frozenset` (six members), never a plain `set` like its neighbour
  `DURABLE_HYDRO_SUCCESS_STATUSES`. The alias stays a plain
  `_HYDRO_RUN_CODE_CLEARING_STATUSES = HYDRO_RUN_CODE_CLEARING_STATUSES`
  assignment (single `Name` target) so the inventory scan still classifies it
  as a constant with consumer `{"_downstream_recorded_error_code"}`; the new
  name is NOT added to `scheduler_state_compat.py`'s re-export list.
- `tests/test_retry.py:178-181` assertions stay true unchanged (the
  scheduler set still holds `"complete"`; `retry` still has no
  `DURABLE_HYDRO_SUCCESS_STATUSES`).

## Decisions

### D1 (#1661) — delete the flag, not just its default

Removing the parameter (rather than defaulting it to `False`) leaves no
misleading surface: a future caller cannot re-enable an unfingerprinted
model-level store by accident. The stub at
`tests/test_gateway_reconcile_file_cohort_identity.py:766-771` (declares the
kwarg with default `True`, asserts `is False`) drops the parameter and the
assertion — after the change production passes no kwarg, so the old assertion
would fail on the default. Spec note: `pipeline-job-persistence`'s "any flag
that governs whether direct records participate SHALL retain its meaning
unchanged in the narrowed read" binds the narrowed single-row replay
(`_iter_pipeline_job_records_scoped`); the batch model-row reducer is not that
replay, and its only ever-executed meaning (`False`) is what remains.

### D2 (#1659) — delete the locked wrapper

The lock acquisition contract converges on the write-lane entry points that
already hold `_write_lock`. The five tests run single-threaded; the lock
contributes no observable semantics to them, so pointing them at
`_next_sequence_unlocked` keeps every assertion verbatim. The archived
mention (`.../2026-08-18-journal-containment-aware-existence-probe/tasks.md:35`)
is a historical record of that change's test surface and is not edited.

### D3 (#1762) — ruling: the analysis branch is unreachable; delete it

Evidence: `_validate_pipeline_job_identity` guards every `pipeline_job`
write and read (replay, per-record apply, flat direct read, pre-write mint — the issue's
`:4533` is its own older-master frame; at `4f3fd89a` the call sites are
`:6207 :6255 :8827 :8985`), and both of its branches accept only
`fcst_{src}_{cycle}_{model}` / `cycle_{src}_{cycle}[_suffix]`
(`:14355-14366`, `:14367-14377`). An `analysis_*` run id raises
`file_journal_run_mismatch` on either branch, so no `pipeline_job` row
carrying one can be written or read, and `query_pipeline_jobs_by_run` /
`_cycle_scope_from_idempotency_key` can never need the derivation. The
ruling is made durable by tests, not by this file: (a) both validator
branches reject an analysis run id; (b) `_cycle_scope_from_file_run_id("analysis_era5_2026010100_2026010200_model_x")`
returns `None` (fall-open) — this is the red command for the removal (it
returns `("ERA5", 2026-01-01T00Z)` before the change); (c) the existing
`test_run_identity.py` analysis case stays green. The regex stays in
`run_identity.py` because `parse_run_cycle` → `retention.py:276` is live.

### D4 (#1763) — delete the function; the comment states the production shape

`reservation.py:45-46` becomes: keys are `run_id:stage[:suffix]`, minted by
`chain_runtime_utils._cycle_stage_idempotency_key`; the safe charset guard is
unchanged. The module docstring (`:21-24`) spelled the deleted shape too and
is rewritten to the same production shape (round-2 cand-07 — the first cut
scoped the comment fix to `:45-46` only); the phantom-shape mention at
`chain_runtime_utils.py:472` is dropped in the same pass.

### D5 (#1581) — one definition, `"complete"` kept, parity locked

Reachability ruling for `"complete"` as a `hydro_run.status`:

- **DB lane: dead.** `hydro.run_status` is a closed enum
  (`db/migrations/000003_enums.sql` + `000013` adds `pending`; no later
  `ADD VALUE` on `run_status`), and `has_completed_pipeline` compares
  `status::text = ANY(...)` — the fourth member can never match.
- **Journal lane: not produced, not validated, test-face reachable.**
  Production writers emit `created` (`:2307`, `:2344`) and the
  `update_hydro_run_status` literals `staged/submitted/running/succeeded/parsed/failed/pending`;
  `append_historical_hydro_run` imports enum-valued DB rows. But
  `_validate_hydro_run_identity` (`:14238-14263`) checks identity fields
  only and never constrains `status`, and the journal test construction
  face writes `hydro_status="complete"`
  (`tests/test_scheduler_generation.py:3116` default,
  `tests/test_scheduler_backfill.py:1291`, and 19 more constructions in
  `tests/test_file_orchestration_journal.py`, e.g. `:472 :627 :891 :5545 :6136`);
  the issue's first comment measured 13 failures on removal across five
  scheduler suites only, so the real blast radius of removal is larger.

Therefore `"complete"` stays. The issue's acceptance bullet "every member is
in the `hydro.run_status` table" is met with one named exception: the parity
test asserts `DURABLE_HYDRO_SUCCESS_STATUSES - {"complete"} <= enum_members`
**and** `"complete" not in enum_members`, so `"complete"` is pinned as the
documented journal-lane-only member and any other out-of-enum member turns
the test red. This is a recorded deviation from the bullet's literal text,
justified by the issue author's own comment withdrawing the "both lanes
unreachable" premise and by acceptance bullet 6 (verbatim-identical
decisions).

Placement: `scheduler_state_types.py` imports only stdlib, so `chain.py`,
`chain_repository.py` and the journal can import from it with no cycle.
`chain.py` and `chain_repository.py` bind `COMPLETED_HYDRO_STATUSES` to the
same object (import + alias, not a copy). `ACTIVE_HYDRO_STATUSES` stays a
literal in each module (membership differs), each carrying a one-line
comment naming the divergence as unadjudicated and pointing at the parity
test. The code-clearing set moves to `scheduler_state_types.HYDRO_RUN_CODE_CLEARING_STATUSES`;
`scheduler_state_failure._HYDRO_RUN_CODE_CLEARING_STATUSES` aliases it and
the journal at `:2485` imports it.

Parity test (`tests/test_hydro_status_set_parity.py`), seams: module
attributes and two behavior probes.

- identity: `chain.COMPLETED_HYDRO_STATUSES is scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES`,
  same for `chain_repository`, `chain_forecast_trigger._completed_hydro_statuses() is ...`,
  and — because the spec names them as consumers and a from-import rebinding
  is invisible to the alias checks (round-1 cand-02) —
  `file_orchestration_journal.COMPLETED_HYDRO_STATUSES is ...` (journal `:72`,
  consumers `:1282`/`:1363`) and
  `scheduler_state_decision.DURABLE_HYDRO_SUCCESS_STATUSES is ...` (`:228`).
- inline site consults the shared object (durable set only — it is a mutable
  `set`): add a sentinel member to `DURABLE_HYDRO_SUCCESS_STATUSES` inside a
  `try/finally` that discards it; while present,
  `scheduler_state_failure._durable_shud_output_exists({"hydro_status": sentinel})`
  is `True`, afterwards `False`.
- code-clearing set (a `frozenset`, so no mutation probe): identity only —
  `journal_module.HYDRO_RUN_CODE_CLEARING_STATUSES is scheduler_state_types.HYDRO_RUN_CODE_CLEARING_STATUSES`
  and `scheduler_state_failure._HYDRO_RUN_CODE_CLEARING_STATUSES is ...`,
  plus `isinstance(..., frozenset)` and the six-member value.
- enum membership: parse `hydro.run_status` members by sweeping every
  `db/migrations/**/*.sql` for the single `CREATE TYPE hydro.run_status`
  block and every `ALTER TYPE hydro.run_status ADD VALUE` (today `000003`
  plus `000013`; round-2 cand-11 — hardcoding the two files left a future
  `ADD VALUE 'complete'` migration invisible, and the selector now routes
  `db/migrations/*.sql` to the parity suite);
  assert `ACTIVE_HYDRO_STATUSES` (all three) `<= enum`,
  `DURABLE - {"complete"} <= enum`, `"complete" not in enum`,
  `HYDRO_RUN_CODE_CLEARING_STATUSES - {"complete"} <= enum`.
- divergence lock: `chain.ACTIVE_HYDRO_STATUSES == chain_repository.ACTIVE_HYDRO_STATUSES == scheduler_state_types.ACTIVE_HYDRO_STATUSES - {"pending"}`.
- Manual counter-proof recorded in the PR body: temporarily add a fake member
  to one alias site → the identity assertion (or the enum assertion) fails.

## Seams under test

- `journal_module._cycle_scope_from_file_run_id` and
  `journal_module._validate_pipeline_job_identity` (module-level functions;
  the existing narrowing tests already target them).
- `FileOrchestrationJournalRepository._next_sequence_unlocked` (existing
  containment tests, repointed).
- `FileOrchestrationJournalRepository._cycle_rows_by_model_unlocked` (existing
  accounting test) and the `batch_rows` stub seam in the cohort-identity test.
- Module attributes of `scheduler_state_types`, `chain`, `chain_repository`,
  `scheduler_state_failure`, `file_orchestration_journal`, and the
  `chain_forecast_trigger._completed_hydro_statuses` seam.
- `db/migrations/*.sql` as the enum oracle (text parse, not a DB).

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — private helpers and module constants; no route, CLI, or exported package API changes.
- Config / project setup: not selected — no config surface.
- File IO / path safety / overwrite: not selected — no path handling changes; the journal's read/write frames are untouched.
- Schema / columns / units / field names: not selected — no schema or field change; the enum is read as an oracle only.
- Auth / permissions / secrets: not selected — none touched.
- Concurrency / shared state / ordering: **selected** — removing an unfingerprinted cache store and a lock-taking wrapper; evidence: existing `_cycle_rows_cache` sweep and containment tests stay green, no new lock acquisition is introduced, every `_next_sequence_unlocked` call still happens under the write lane's lock.
- Resource limits / large input / discovery: not selected — no bound changes.
- Legacy compatibility / examples: **selected** — name-import and `getattr` seams, the compat re-export list, the private-name inventory tables; evidence: import smoke on every named module and the parity test's identity assertions.
- Error handling / rollback / partial outputs: not selected — no error contract changes; `file_journal_run_mismatch` on analysis run ids is pinned, not changed.
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: **selected** — the #1155 wording, the reservation comment, the `_cycle_scope_from_file_run_id` docstring; evidence: reviewer reads the corrected text against the rulings above.

Domain packs (NHMS profile): Slurm production lifecycle — not selected (no scheduling change); Run manifest / QC provenance — not selected; the remaining geospatial / time-series / numerical / PostGIS / provider / display packs — not selected (untouched).

## Required evidence

- Red proof (#1762): `_cycle_scope_from_file_run_id("analysis_era5_2026010100_2026010200_model_x")` → `("ERA5", 2026-01-01T00Z)` at `4f3fd89a`, `None` after.
- Red proof (#1661 stub): the cohort-identity stub's old `assert include_direct_jobs is False` fails against the new production call (TypeError/AssertionError) before the stub edit — recorded, then the stub is fixed.
- Parity counter-proof (#1581): fake member on one alias → red; recorded.
- `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_gateway_reconcile_file_cohort_identity.py tests/test_gateway_reconcile_comment_accounting.py tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_retry.py tests/test_run_identity.py tests/test_hydro_status_set_parity.py tests/test_scheduler_generation.py tests/test_scheduler_backfill.py` green.
- `uv run ruff check .` clean; `openspec validate orchestrator-dead-surface-prune --strict --no-interactive`.
- Acceptance greps (see tasks 0.x) with the pre-recorded residuals.
- node-27 real-DB run of the same suites at the final head (verification-matrix row for Python/shared helper is local pytest; the node-27 run is the project's iteration oracle).

## Review focus

- Every removed line was genuinely unreachable (no caller relied on the default of `include_direct_jobs`, no caller of `_next_sequence`, no analysis-shaped `pipeline_job` row can be minted or read, no `candidate_idempotency_key` reference).
- Membership of every hydro status set is unchanged on every site; aliases are same-object, not copies.
- The `"complete"` ruling and the `"pending"` divergence are stated accurately in comments and pinned by the parity test; no test/spec was weakened.
- The `_materialize_latest_unlocked` flag and `_cycle_rows`' fingerprinted store are untouched.
