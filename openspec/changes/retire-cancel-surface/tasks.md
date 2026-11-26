# Tasks: retire-cancel-surface (#3394 slice 1)

## Must preserve

- Scheduler cancellation, the Slurm gateway's cancel route and `slurm.cancel_job`, the `cancelled` status and every
  enum/filter showing it, the retry 409 stub and its tests, the `compute_control` role.
- `PipelineStore.insert_event`, `query_jobs_by_run`, `update_job_status`; the symbols listed in `design.md`.
- The display-lane negative checks (no retry or cancel control; the C4 DOM guard; the live e2e non-GET guard).
- Every property a vehicle test proved, unless the PR states that it leaves with the route and why (D2).
- Entropy hard gate: 0 failing, findings unchanged except those that name deleted code.

## 1. Fixture

- [x] 1.1 Fixture review (`pass`) and `openspec validate retire-cancel-surface --strict --no-interactive`.

## 2. Commit A — vehicle tests move while the route exists

- [ ] 2.1 For each of the ten vehicle tests named in proposal D2: one of the three outcomes of D2. A table in the report: old test id → outcome → new test id or the cited duplicate
      (assertion by assertion) or the reason the property leaves.
- [ ] 2.2 Red run per moved test, restored: the audit record or policy decision dropped; for the `error_message` pin
      the redaction in `_job_payload` removed.
- [ ] 2.3 No cancel test is deleted in this commit; the touched test files are green with the route in place.

## 3. Commit B — backend

- [ ] 3.1 `apps/api/routes/pipeline.py`: the route, `cancel_run`, its dependency chain, every helper and constant only
      it uses (each "only" shown by grep); the cancel branch of the manual-action suggestion; the decisions branch of
      the route module's `_ok` when no caller is left.
- [ ] 3.2 `apps/api/response_models/pipeline.py`, `apps/api/openapi_patching_pipeline.py` (and the component that
      becomes unreferenced), `apps/api/openapi_patching_security.py`, `apps/api/readonly_validation_probe.py`,
      `packages/common/auth_policy.py`.
- [ ] 3.3 `packages/common/status_vocab.py`: the two sets of D4, with their parity rows and allowlist entries; or the
      reader that keeps one alive, reported.
- [ ] 3.4 `openapi/nhms.v1.yaml` regenerated and `apps/frontend/src/api/types.ts` regenerated (`generate:api`);
      `check:api-types` and the OpenAPI drift test pass.
- [ ] 3.5 The D5 test: in the three service roles with operator, viewer and no credentials the path answers 405 with
      `Allow` equal to `{GET}` (compared as a set of methods), equal in status, `Allow` and body to the answer of an unregistered POST path under `/api/v1/` in the same
      combination, with no gateway constructed and no state written. Red run: the route restored in a scratch edit →
      the test goes red.

## 4. Commit B — frontend

- [ ] 4.1 `JobsTable.tsx`: the cancel button, its request, pending state and role plumbing, and everything that only
      served them; the action column keeps the log modal and the diagnostics actions.
- [ ] 4.2 `MonitoringPage.tsx` (the prop, `controlMutationsEnabled`, the disabled-controls text); `stores/auth.ts`
      (`canUseDevRoleActions`, `OperatorRole`, `isTestMode` when nothing else uses them, each shown by grep).
- [ ] 4.3 Tests: the cancel tests of `JobsTable.test.tsx` go; a test that an operator on a `compute_control` API sees
      no cancel and no retry control and that no request to either path is sent; `c4DisplayLane.test.tsx`,
      `e2e/monitoring.mocked.spec.ts` and `e2e/support/monitoring.mocked.ts` follow; the display-lane negative checks
      stay.

## 5. Commit B — pytest

- [ ] 5.1 The cancel-only tests go (`tests/test_retry_cancel_consistency.py`, `tests/test_monitoring_api.py` and the
      other files of recon §4); the retry tests and `test_enum_sets_match_migration` stay; helpers that
      `tests/test_response_model_preservation_pipeline.py` imports stay or move with their user.
- [ ] 5.2 The two route-count pins follow (33 → 32, 37 → 36), each with the route named in the test's comment.
- [ ] 5.3 Tests of the readonly validation probe list, the protected-operation set and the RBAC action ids follow.

## 6. Completeness

- [ ] 6.1 After commit B: `git grep` for `runs/{run_id}/cancel`, `/cancel"`, `cancel_run`, `CancelRun`,
      `pipeline.cancel_run`, `display_cancel_manual_action`, `HYDRO_RUN_TERMINAL_UI`, `CYCLE_TERMINAL_UI` over `apps`,
      `packages`, `services`, `workers`, `scripts`, `schemas`, `tests`, `infra`, `openapi`, `openspec/specs` and
      non-history docs. Every hit classified (stays and why / goes); the table in the report.
- [ ] 6.2 `schemas/**` and any receipt validator that enumerates the manual-action probes or the protected operations:
      follow or reported as not affected, with the file read.

## 7. Docs (orchestrator)

- [ ] 7.1 Every non-history doc line of recon §5; `docs/runbooks/node22-control-plane-manual-recovery.md` (the stuck
      `pending` row step); the statements that the API's pipeline routes write (D6); `apps/api/AGENTS.md`,
      `apps/frontend/AGENTS.md` and their sources.
- [ ] 7.2 Purposes of `display-control-mutation-guard`, `ops-monitoring-api`, `monitoring-migration`,
      `production-state-machine-contract`, `pipeline-control-state-consistency` after the archive.

## 8. Evidence Floor

- [ ] 8.1 Local: touched pytest files and every reader of an edited file; frontend `pnpm test` of the touched files,
      `tsc`, lint, `pnpm build`, the mocked `/monitoring` spec; `ruff check --no-cache .`; entropy hard gate.
- [ ] 8.2 node-27 (detached worktree, no database): the touched backend test files; whole-tree collection without
      error.
- [ ] 8.3 Code review (two seats: backend contract and auth; frontend and test evidence); CI green including every
      mocked e2e shard.

After the merge, not a task: the display API restart and the frontend rebuild on node-27 (owner go-ahead), then a live
check that the path answers 405 and `/monitoring` shows no cancel control.
