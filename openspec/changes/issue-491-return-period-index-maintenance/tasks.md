## 1. OpenSpec Fixture

- [ ] 1.1 Create proposal/design/tasks/spec delta for #491.
- [ ] 1.2 Pass focused fixture review, including high-risk Invariant Matrix review.
- [ ] 1.3 Run `openspec validate issue-491-return-period-index-maintenance --strict --no-interactive`.

## 2. Audit And Plan Tooling

- [ ] 2.1 Add an operator-facing script/command that emits `flood.return_period_result` index inventory, root/chunk size, `pg_stat_user_indexes`, and relation-size SQL evidence.
- [ ] 2.2 Add hot-path query-plan probes or generated `EXPLAIN (ANALYZE, BUFFERS)` SQL for summary, ranking/segments, timeline, GeoJSON fallback, MVT selected identity, valid-time discovery, and latest-ready-run quality behavior.
- [ ] 2.3 Classify known indexes from migrations 000015, 000020, 000021, 000031, and 000034 as keep/drop/rebuild/replace/investigate with reason and hot-path mapping.
- [ ] 2.4 Generate manual maintenance SQL/runbook output that includes `lock_timeout`, transaction guidance, failure recovery, pre/post size SQL, and explicit no-auto-production-execution warnings.
- [ ] 2.5 Add connection-mode evidence: readonly/audit mode may collect report data and generate templates; writer/maintenance mode still must not execute destructive DDL unless an explicit manual artifact is requested.

## 3. Safety And Error Handling

- [ ] 3.1 Ensure destructive SQL is never executed by default and requires an explicit operator-controlled file/output workflow.
- [ ] 3.2 Ensure report output paths are no-clobber or explicitly acknowledged, and partial output failures surface stable errors.
- [ ] 3.3 Ensure Timescale optional metadata failures degrade to explicit unavailable evidence without hiding root-table audit results.
- [ ] 3.4 Ensure credentials/secrets are not printed in reports or generated SQL.
- [ ] 3.5 Ensure destructive production DDL and space-recovery actions are represented only as manual plan steps with approval, lock-timeout, rollback/retry, and before/after evidence requirements.

## 4. Documentation

- [ ] 4.1 Update production runbook guidance for post-#490 return-period index maintenance and space recovery.
- [ ] 4.2 Document that DELETE row cleanup does not imply disk-space release and that index/space recovery requires a separate maintenance window.
- [ ] 4.3 Document rollback/retry steps for failed index maintenance and how to capture before/after DB/table/chunk/index size evidence.

## 5. Tests And Verification

- [ ] 5.1 Add unit tests with a mock catalog containing `return_period_result_null_return_period_run_idx` and `return_period_result_null_warning_level_run_idx`; expected output marks them drop/investigate candidates and never executes generated DDL.
- [ ] 5.2 Add unit tests with a mock catalog containing the known 000015/000020/000021/000031 indexes; expected output maps each index to summary, ranking/segments, timeline, map, MVT selected identity, valid-time discovery, run-quality, or investigate evidence.
- [ ] 5.3 Add unit tests for hot-path probe generation. Inputs: sample `run_id`, `duration`, `valid_time`, basin/network identity, segment id, and bbox. Expected output includes parameterized `EXPLAIN (ANALYZE, BUFFERS)` SQL for summary, ranking/segments, timeline, GeoJSON fallback, MVT selected identity, valid-time discovery, and latest-ready-run quality behavior.
- [ ] 5.4 Add unit tests for connection-mode guardrails. Inputs: readonly/audit mode and writer/maintenance mode without explicit manual artifact request. Expected output collects/generates evidence but records that destructive DDL was not executed.
- [ ] 5.5 Add unit tests for output-path safety. Inputs: existing report path without overwrite acknowledgement and simulated partial write failure. Expected output is a stable error and no successful report marker.
- [ ] 5.6 Add unit tests for Timescale metadata fallback. Input: chunk metadata query raises an expected database error. Expected output includes root-table evidence and a chunk evidence unavailable reason.
- [ ] 5.7 Add unit tests for generated SQL guardrails. Expected output contains `lock_timeout`, manual approval comments, pre/post size SQL, rollback/retry notes, and no embedded database credentials.
- [ ] 5.8 Update targeted CI selection and tests so #491 changes run focused Python tests rather than the full suite.
- [ ] 5.9 Run focused tests for DB ops tooling, flood API/tile hot-path compatibility tests as needed, `ruff`, `openspec validate`, and `git diff --check`.
