# Tasks — retire-manual-retry-submit-lanes (#3157, PR 1 of 2)

- [x] 1.1 Route: add a fail-closed dependency after `require_retry_control_action`, so the order stays 400, then
  RBAC 401/403, then the display 409. It raises 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` with the What 1 details and
  constructs no DB session, `RetryService` or gateway.
  - Remove `get_retry_service` from the retry route's dependency graph, but keep the function: the node-27 C2 read-only
    probe and four tests use it.
  - Update `_manual_action_suggestion("retry")`.
  - Keep the route path, `operation_id`, `response_model` and declared responses, so OpenAPI stays byte-identical.
- [x] 1.2 Delete `RetryService.attempt_manual_retry`, `FileJournalRetryService.attempt_manual_retry`, the
  `ManualRetryService` protocol and the route's success / 503 branches. Do not delete helpers that other code still
  reaches; PR 2 handles the dead closure.
- [x] 1.3 Tests:
  - build the per-test delete/reseed/trim table (What 4) across every file in E2 plus any other that reaches the retired
    methods or the route's success path;
  - delete or reseed accordingly;
  - add the pins with red-on-old evidence;
  - add the `FAIL_CLOSED_ROUTES` exemption in `tests/test_response_model_preservation.py` and delete the retry sample;
  - check each edited file's line count and guard status first. `tests/test_two_node_e2e_evidence.py` (9975 lines) is
    not excluded and must not be edited.
- [x] 1.4 Selector: follow the deletions in `scripts/select_ci_tests_tables.py` / `_rules.py`, the frozen lists and the
  pinned node ids. Route the new pin tests. Update `tests/fixtures/file_journal_cluster_map.json` and
  `tests/status_literal_allowlist.json` if their subjects change.
- [x] 1.5 Docs: rewrite the lines listed in What 6, leave the display refusal probes alone, and add the
  `node22-control-plane-manual-recovery.md` section required by the acceptance criteria. Rewrite the stale
  docstrings and comments listed in E5.
- [x] 1.6 Follow-up issue (orchestrator, #3179): remove the `JobsTable` retry button, the `retryRun` OpenAPI operation, the
  `RetryRunResult` schema and the route.

## PR 2 — dead closure (closes #3157)

- [x] 2.1 Compute the dead closure left by PR 1: every function, method, class, constant and module-level name in
  `apps/api/routes/pipeline.py`, `services/orchestrator/retry.py` and `services/orchestrator/file_orchestration_journal.py`
  (and any module they alone feed) that is reachable only from the deleted methods or from other members of the
  closure. Start from the PR 1 list (`pipeline._api_error`; `submission_runtime_root_resolution` on both lanes;
  `retry.py` `_submit_retry_job`, `_record_retry_submission_*`, `_locked_retry_job`, `_resolve_retry_model_id`,
  `_create_pending_manual_retry_job`, `expire_stale_retries`, `_attach_retry_runtime_root_*`; journal
  `_manual_retry_submission_request`, `_submit_file_manual_retry_job`, `_record_manual_retry_submission_*`,
  `_resolve_file_retry_runtime_roots`, `_file_retry_runtime_root_candidates`, `_file_retry_provenance_job_ids`,
  `_file_retry_previous_job_*`, `_next_file_manual_retry_job_id_for_run` and its pending-row creator) and confirm each
  member with a repo-wide reference scan (attribute access, `getattr` strings, monkeypatch targets, `__all__`, facade
  re-exports, fixtures such as `tests/fixtures/file_journal_cluster_map.json`). A name with any live reference stays.
- [x] 2.2 Delete the closure, `expire_stale_retries` and its two tests, the dead allocator monkeypatch in
  `tests/test_file_journal_read_blocked_consumers.py`, and any test that exercises only closure members. Imports that
  become unused go too.
- [x] 2.3 Rewrite the `services/orchestrator/public_evidence.py` module docstring so it no longer names the retry 503 as a
  consumer, and any other docstring/comment that names a deleted member as live. The recovery runbook line that says
  `expire_stale_retries` has no production caller is reworded to say the expiry path was removed (PR 2).
- [x] 2.4 Selector, `__all__`, facade-surface tests, cluster map and status-literal allowlist follow the deletions.

## PR 2 Evidence Floor

- [x] E7 `uv run ruff check --no-cache .`; entropy hard gate `hard_gate_failing_count 0`. — pass; 0.
- [x] E8 AST evidence: for each edited source module, the set of top-level/class-level definitions on HEAD equals the
  set on `origin/master` minus the deleted closure, and every surviving definition's source text is byte-identical
  (except docstring/comment edits named in 2.3). A repo-wide `git grep -w` for every deleted name returns only
  `openspec/changes/archive/`, this change directory, same-name definitions in other modules, historical records
  (`docs/review-loop-log.jsonl`, the ADR 0009 census snapshot, quoted historical error codes) and REMOVED requirements
  in living specs that this change's archive drops.
  — AST: removed set equals the 103-name closure, survivors identical except the 2.3 docstring/comment edits;
  grep: 0 unclassified hits at 9ccb6a5a9.
- [x] E9 Tests green locally and in a node-27 temporary worktree: the selector set for the diff plus
  `tests/test_retry.py`, `tests/test_retry_cancel_consistency.py`, `tests/test_file_orchestration_journal.py`,
  `tests/test_file_orchestration_journal_facade.py`, `tests/test_file_journal_read_blocked_consumers.py`,
  `tests/test_monitoring_api.py`, `tests/test_retry_route_fail_closed.py`, `tests/test_api_contract*.py`,
  `tests/test_response_model_preservation*.py` and `tests/test_select_ci_tests.py`.
  — 157 files: local 8637 passed / 3 skipped; node-27 at f9dd24aad 8642 passed, 0 leftover `nhms_it_*` DBs. The
  review fixes after it (one re-pinned upsert test, two docstrings) ran locally: journal suite 644 passed / 2 skipped,
  new DB-free pin 1 passed; the upsert pin is red under the keep-merge mutation.
- [x] E10 `git diff origin/master -- openapi/ apps/frontend/src/api/types.ts` is empty;
  `openspec validate retire-manual-retry-submit-lanes --strict --no-interactive`. — empty; valid.
