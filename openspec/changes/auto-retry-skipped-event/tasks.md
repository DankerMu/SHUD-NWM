# Tasks: auto-retry-skipped-event

Fixture level: expanded · Repair intensity: medium · Issue #1314

## Risk packs (considered)

- Schema / columns / units / field names: **selected** — details_json payload
  keys/reason enum are the contract; spec-parse alignment test required.
- Error handling / rollback / partial outputs: **selected** — the payload
  rides the permanent-failure path; discrimination vs limit-exhausted is the
  governing rule.
- Legacy compatibility / examples: **selected** — existing permanently_failed
  event consumers/tests must keep passing (additive-only details).
- Documentation / migration notes: **selected** — db-free plane disposition
  is a documented (spec-delta) deliverable.
- Public API / CLI / script entry: not selected — no route/CLI change.
- Config / project setup: not selected — none.
- File IO / path safety / overwrite: not selected — journal insert reuse.
- Auth / permissions / secrets: not selected — none.
- Concurrency / shared state / ordering: not selected — no new state
  transitions; decision sets untouched.
- Resource limits / large input / discovery: not selected — none.
- Release / packaging / dependency compatibility: not selected — none.
- Domain packs: not selected — orchestration-internal observability; no
  geospatial/forcing/SHUD/DB-domain/display surface (Slurm lifecycle pack
  not selected: classification behavior untouched, evidence additive).

## Implementation tasks

- [x] 1. `retry.py`: `auto_retry_skipped_details(error_code)` helper —
  non-transient → `{"auto_retry_skipped": True, "reason":
  "non_transient_error", "error_code": code}`; neither-list (and not
  None/empty) → reason `unknown_error_code_defaulted_non_transient`;
  transient or None/empty → `None`.
- [x] 2. `retry.py` `mark_permanently_failed`: merge non-None helper output
  into the existing `permanently_failed` details; unknown branch logs the
  exact spec warning once per mark.
- [x] 3. `file_orchestration_journal.py`: BOTH production points merge the
  helper output (no second reason literal) —
  (i) non-master branch details dict (~:6930-6939);
  (ii) master branch `_mark_master_permanently_failed` `event_details`
  (~:6959-6968, appended by the repository at ~:2481). Warning follows the
  append-gated rule (design decision 3): no orphan warning when the
  repository returns `missing`/`stale`/`idempotent`.
- [x] 4. Tests (both planes parameterized):
  (a) every code parsed from the spec's non-transient list produces the
  non_transient_error payload; (b) an unknown code produces the
  defaulted reason + warning log (caplog, logger-pinned); (c) transient
  code with exhausted budget → NO `auto_retry_skipped` key; (d)
  None/missing error_code → no key; (e) payload consistency with
  `failure.retryable`/`limit_exhausted`; (f) reason literals single-source
  (grep/AST: defined once under services/); (g) master-row case:
  `_terminally_failed_cohort_master`-shaped fixture + non-transient code →
  the master's `permanently_failed` event details contain the payload
  (fixtures exist in tests/test_file_orchestration_journal.py; sibling
  decline branch at tests/test_orchestration_chain.py:13504);
  (h) stale/duplicate master mark (repository returns without appending) →
  event count unchanged AND warning count unchanged (no orphan warning).
- [ ] 5. Spec delta (MODIFIED Retry Guard requirement): plane disposition +
  limit-exhausted discrimination + event-reuse scenarios.

## Required evidence

- [x] `uv run pytest -q tests/test_retry.py tests/test_file_orchestration_journal.py tests/test_production_scheduler.py` — green
- [x] Red proof: new tests red against pre-change source (batched output)
- [x] Per-plane event-level assertions green: DB plane (test_retry.py) and
  BOTH file-journal production points (non-master + master row) each have a
  test asserting the payload inside an actually-appended event — grep alone
  does not discharge this row
- [x] `grep -rn "auto_retry_skipped" services/` — non-empty (supplementary)
- [x] `grep -rn "defaulted to non-transient" --include="*.py" services/` — non-empty
- [x] `uv run ruff check .` (tracked tree) — clean
- [x] `openspec validate auto-retry-skipped-event --strict --no-interactive` — valid
- [x] Classification sets untouched: `git diff` shows no edits to
  `TRANSIENT_ERROR_CODES` / `NON_TRANSIENT_ERROR_CODES` members
- [x] No new event type: `git diff` adds no `event_type` value

## Non-goals

- spec.md:234 `manual_retry_already_active` (separate issue); #1161 tasks
  5.0 (b)-(e); archived spec copies; any classification change.
