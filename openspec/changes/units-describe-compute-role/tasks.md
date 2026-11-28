# Tasks: the deployed units describe themselves as units of the compute role (#3427)

## Must preserve

- Every directive of every unit other than `Description=`; unit names; drop-ins' content; timers'
  schedules.
- The manifest's schema, entries, order and paths.
- The receipts of 2026-10-16 and 2026-11-26, byte for byte.
- The drift probe and its tests.
- Nothing is started, stopped or restarted on the host; the production checkout is not moved; no
  test mail is sent.

## 1. Repository

- [ ] 1.1 `Description=` of the 16 units and the nine comment lines (`design.md` D1).
- [ ] 1.2 The example unit `infra/systemd/nhms-slurm-gateway.service` (D1).
- [ ] 1.3 `infra/systemd/node22/MANIFEST.json`: 17 sha256 values (D2).
- [ ] 1.4 `tests/test_node22_unit_tracking.py`: the third receipt and the rule of D3 as module-level
  functions with synthetic cases; the module docstring (it says "the two receipts (which do not
  overlap)"); the test `test_the_two_receipts_record_disjoint_sets_…` renamed to what it now
  asserts; the `test_node22_*` names kept; the red runs R1 to R7.
- [ ] 1.5 The spec delta of `node22-unit-tracking` (D6).
- [ ] 1.6 `infra/env/README.md`: the sentence that names the receipts (D7).

## 2. Host step and receipt

- [ ] 2.1 Before the host step, on the commit that holds the 17 files (D4): the hard-gate
  comparison; the tests of the Evidence Floor green locally and on node-27; the review; CI green
  on the PR.
- [ ] 2.2 The host step of D4, at a scheduler pass boundary.
- [ ] 2.3 `docs/runbooks/receipts/2026-11-28-w3-scheduler-and-gateway-units-snapshot.md` with the
  values read on the host (exactly two rows of the parsed shape).
- [ ] 2.4 `docs/runbooks/receipts/2026-11-28-issue-3427-unit-descriptions-redeployed.md`: the
  operator receipt of the host step (D8).

## 3. Archive

- [ ] 3.1 The change archived in the PR's last push; the Purpose line edited; the main spec compared
  with its expectation.
- [ ] 3.2 Task 9.2 ticked in `openspec/changes/sweep-compute-role-narration/tasks.md`.

## Evidence Floor

- The unit diff: for each of the 18 unit files, every changed line is a `Description=` line or a
  comment line (`git diff -U0`).
- Red runs R1 to R7 (D3), each with its command and the failing assertion.
- Green on the commit that holds the 17 files, before the host step. Locally:
  `tests/test_node22_unit_tracking.py`, `tests/test_node22_unit_drift_check.py`,
  `tests/test_node22_unit_failure_alert.py`, `tests/test_slurm_gateway_deployment_contract.py`,
  `tests/test_scheduler_refresh_deployment_contract.py`, `tests/test_compute_system_units_tracking.py`,
  `tests/test_node22_entrypoint_invariant.py`, `tests/test_node22_entrypoint_invariant_python_scan.py`,
  `tests/test_node22_scheduler_stall_health.py`, `tests/test_node22_refresh_timer_health_installer.py`,
  `tests/test_node22_refresh_timer_health_runbook_and_env.py`, `tests/test_node22_state_index_auto_prune.py`,
  `tests/test_node22_model_succession_timer_and_refresh.py`, `tests/test_role_boundary_static.py`,
  and every `tests/test_entropy_audit_*.py`. On node-27: those, and every other test file that
  mentions `systemd`, `.service` or `.timer` (`git grep -l -E 'systemd|\.service|\.timer' -- 'tests/test_*.py'`). In CI: the full
  sharded suite on the PR, green before the host step.
- Hard gate on that commit, before the host step: the finding multiset (check id, path, line)
  identical to master's, or each difference listed and judged (a comment that changes its number
  of lines moves the findings below it).
- The operator receipt of the host step: both probe summaries, each installed file with its sha256,
  the backup directory, the state of the timers and the gateway before and after, the new
  `Description` of a sample of units. No account name, address or credential in it.
- `openspec validate units-describe-compute-role --strict --no-interactive`; after the archive the
  spec's validation report equal to the one before except for what the delta changes, and
  `openspec validate sweep-compute-role-narration --strict --no-interactive`.
- `narration_count.py` plain and `--tokens` before and after; every moved name token explained.
- Hard gate again on the final head.
- `uv run ruff check --no-cache .`; markdownlint on the edited documents; CI green on every shard.

Suggested fixture level: expanded - deployed unit bytes on the production host.
Minimal mergeable slice: atomic - the tracked bytes, the manifest, the receipts and the installed copies have to agree when it merges.
