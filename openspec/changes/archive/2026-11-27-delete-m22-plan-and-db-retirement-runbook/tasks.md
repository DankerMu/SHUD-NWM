# Tasks: delete the M22 E2E plan and the DB retirement runbook (#3406)

Must preserve: every assertion of the three tests; the commands of the snippet, token for token;
the receipt of 2026-06-29; history files; the other readers of `infra/env/README.md` stay green
unedited (`tests/test_env_templates.py`, `test_node22_scheduler_env_drift_table.py`,
`test_node22_unit_failure_alert.py`, `test_node22_unit_tracking.py`,
`test_node22_env_backup_sweep.py`, `test_slurm_gateway_deployment_contract.py`, and the two tests
that anchor on its authority heading).

## 1. Documents (orchestrator)

- [x] 1.1 `infra/env/README.md`: a section with the snippet, placed directly before the heading
  "node-22 unit → EnvironmentFile authority (#1694)" (after the list of published-artifact
  variables, so that list stays where it is). The section says where `EVIDENCE_ROOT` comes from
  (a run directory under one of the approved evidence roots the page already names), that
  `NHMS_READONLY_DB_VALIDATION_RUN_ID` is the business run and not the evidence bundle, and that
  the four per-source exports are optional overrides. The snippet's check of the plan's shared
  `RUN_ID` does not move: nothing in the block uses it.
- [x] 1.2 `git rm` the two runbooks.
- [x] 1.3 The five pointer sentences and links; the two inventory mentions.
- [x] 1.4 Spec delta: `evidence-boundary-hardening`, one requirement removed and added under a new
  title.

## 2. Tests (implementer)

- [x] 2.1 `tests/test_readonly_db_validation_routes.py`: both tests read `infra/env/README.md` for
  what they read in the plan. No assertion is dropped or weakened; the test that already reads the
  README keeps its three prose assertions.
- [x] 2.2 `tests/test_role_boundary_static.py`: the cleanup-snippet test walks the receipt only; the
  runbook constant goes.
- [x] 2.3 Red runs against the README, each restored, each failing on the assertion named:
  - the `source` line moved above the mode check: the order assertion (mode check < source);
  - only the first validator call given the doubled form
    `--evidence-root "artifacts/two-node-e2e/$EVIDENCE_RUN_ID"`, the other call left as it is:
    the `not in` assertion on that form;
  - the weak line `test "$(stat -c '%a' infra/env/display-readonly-secrets.env)" = "600"` added
    next to the real guard, the guard left intact: the `not in` assertion on that line;
  - the `BLOCKED:` message of the mode check removed: its positive assertion.
  Red run for 2.2: one fail-closed line removed from the receipt's cleanup snippet.

## 3. Evidence

- [x] 3.1 A search for each of the two file names returns only history, the umbrella change's own
  files and the synthetic test inputs (listed in the PR).
- [x] 3.2 No link in a remaining document points at a deleted file.
- [x] 3.3 Hard gate: finding multiset before and after, each difference listed.
- [x] 3.4 `narration_count.py` plain and `--tokens` before and after; moved name tokens listed.

## Evidence Floor

- `tests/test_readonly_db_validation_routes.py`, `tests/test_role_boundary_static.py` and the readers
  of `infra/env/README.md` named above, locally and on node-27; `uv run ruff check --no-cache .`;
  markdownlint on the edited docs; `openspec validate delete-m22-plan-and-db-retirement-runbook
  --strict --no-interactive` before the archive; `openspec validate evidence-boundary-hardening
  --type spec --strict --no-interactive` and `openspec validate sweep-compute-role-narration
  --strict --no-interactive` after; CI, every shard.
