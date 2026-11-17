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
