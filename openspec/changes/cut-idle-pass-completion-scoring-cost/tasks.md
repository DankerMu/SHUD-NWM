# Tasks: cut the idle pass completion-scoring cost (#3223)

Risk packs: Schema / field names — selected (pass evidence `retention` block changes; E4). Resource limits / large
input — selected (528 candidates x events; E2, E6). Legacy compatibility — selected (test doubles and the compat
wrapper around the decider; E1, E3). Concurrency / shared state — not selected (no cache or shared state added).
File IO / path safety — not selected (no read path touched; D3). Others — not applicable.

## 1. Completion scoring without the per-event evidence copy

- [ ] 1.1 Give `_candidate_state_decision` / `_candidate_state_evidence` the opt-out of D1 (default unchanged; key absent when opted out).
- [ ] 1.2 Use it from `_cycle_completion_verdict` only (D2); the compat wrapper passes it through.
- [ ] 1.3 Confirm by grep and by test that no ladder branch or helper reads `evidence["pipeline_events"]`.

## 2. Retention skips in pass evidence

- [ ] 2.1 Project the retention block written into pass evidence as D4 says; `RetentionResult` keeps its list (D5).
- [ ] 2.2 `_compact_retention` keeps `skipped_by_reason`.
- [ ] 2.3 Report the callers of `RetentionResult.to_dict()`; change none that is not the scheduler pass evidence.

## 3. Tests

- [ ] 3.1 Decision parity test (design.md "Required evidence", first bullet).
- [ ] 3.2 Completion scoring: sanitiser spy + verdict unchanged, including a recalibrated-model cycle.
- [ ] 3.3 Retention block tests (sum, sample filter, cap and overflow count, over-limit compaction).
- [ ] 3.4 Existing completion, backfill, submit-path and retention-evidence tests pass without weakening; any edited
      expectation is listed in the report with the reason.

## 4. Docs and spec

- [ ] 4.1 Update any current doc that tells an operator to read `retention.skipped` from pass evidence.
- [ ] 4.2 Spec deltas; `openspec validate cut-idle-pass-completion-scoring-cost --strict --no-interactive`.
- [ ] 4.3 Archive in the final push.

## Evidence Floor

- [ ] E1 Parity test green; it fails if the opt-out changes `action`, `reason` or any evidence field other than removing `pipeline_events`.
- [ ] E2 Completion scoring makes zero event-sanitiser calls (spy) and returns the same verdicts; the spy test fails when the opt-out is removed from the call site.
- [ ] E3 `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py tests/test_scheduler_pass_status_without_dispatch.py` and the candidate-state / decision test files green; no existing assertion on submit-path `pipeline_events` edited.
- [ ] E4 Retention block tests green; an abnormal skip (e.g. `unparseable_cycle_name`) is visible in `skipped_sample` and in `skipped_by_reason`.
- [ ] E5 `uv run ruff check . --no-cache`; full sharded pytest and Production Topology Hard Gate green on the PR.
- [ ] E6 (post-merge, node-22, recorded on #3223): idle `timing.pass.total_wall_ms`, `journal_read_attribution.totals`, evidence file size before/after; one submitting pass completes normally.
