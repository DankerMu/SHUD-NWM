# Tasks: cut the idle pass completion-scoring cost (#3223)

Risk packs: Schema / field names — selected (pass evidence `retention` block: list narrowed, two additive fields; E4). Resource limits / large
input — selected (528 candidates x events; E2, E6). Legacy compatibility — selected (test doubles and the compat
wrapper around the decider; E1, E3). Concurrency / shared state — not selected (no cache or shared state added).
File IO / path safety — not selected (no read path touched; D3). Others — not applicable.

## 1. Completion scoring without the per-event evidence copy

- [x] 1.1 Give `_candidate_state_decision` / `_candidate_state_evidence` the opt-out of D1 (default unchanged; key absent when opted out).
- [x] 1.2 Pass it unconditionally at both decider calls in `_cycle_completion_verdict` (`:362`, `:444`) and nowhere else (D2); update the two-argument doubles reachable from the discovery slot (`tests/test_file_orchestration_journal.py:7552`, `tests/test_production_scheduler.py:41620`, `tests/test_scheduler_backfill.py:2929`).
- [x] 1.3 Confirm by grep and by test that no ladder branch or helper reads `evidence["pipeline_events"]`.

## 2. Retention skips in pass evidence

- [x] 2.1 Project the retention block written into pass evidence as D4 says, in `_run_retention` after `result.to_dict()`; `RetentionResult` keeps its list (D5).
- [x] 2.2 `_compact_retention` keeps `skipped_summary` and `skipped_omits`.
- [x] 2.3 `RetentionResult.to_dict()` and the standalone receipt (`services/orchestrator/cli.py:223`) stay unchanged; report any other caller of `to_dict()`.

## 3. Tests

- [x] 3.1 Decision parity test (design.md "Required evidence", first bullet).
- [x] 3.2 Completion scoring through the production binding: no `pipeline_events` in the evidence it receives (both call sites), positive control, verdict cases of design.md.
- [x] 3.3 Retention block tests (summary sum and roots, window entries omitted, every other reason listed, `skipped_omits`, over-limit compaction, standalone receipt unchanged).
- [x] 3.4 Existing completion, backfill, submit-path and retention-evidence tests pass without weakening; any edited
      expectation is listed in the report with the reason.

## 4. Docs and spec

- [x] 4.1 `docs/runbooks/two-node-deployment-overview.md:446`: add one sentence on `skipped_summary` / `skipped_omits`; check for other docs that describe `retention.skipped`.
- [x] 4.2 Spec deltas; `openspec validate cut-idle-pass-completion-scoring-cost --strict --no-interactive`.
- [x] 4.3 Archive in the final push.

## Evidence Floor

- [x] E1 Parity test green; it fails if the opt-out changes `action`, `reason` or any evidence field other than removing `pipeline_events`.
- [x] E2 Through `ProductionScheduler._discovery_context()`, the decision evidence completion scoring receives has no `pipeline_events` at either call site, while the default on the same state has it (positive control); verdicts equal the full-evidence verdicts for the cases in design.md. The test fails when the opt-out is removed from either call site.
- [x] E3 `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py tests/test_scheduler_pass_status_without_dispatch.py` and the candidate-state / decision test files green; no existing assertion on submit-path `pipeline_events` edited.
- [x] E4 Retention block tests green: every non-window skip (e.g. `unparseable_cycle_name`, `pipeline_frontier_exempt`) is still listed in `skipped`; `skipped_summary` sums to `counts.skipped`; `tests/test_production_scheduler.py:23797` passes unedited.
- [x] E5 `uv run ruff check . --no-cache`; full sharded pytest and Production Topology Hard Gate green on the PR.
- [ ] E6 (post-merge, node-22, recorded on #3223; pending when this change was archived): idle `timing.pass.total_wall_ms`, `journal_read_attribution.totals`, evidence file size before/after; one submitting pass completes normally.
