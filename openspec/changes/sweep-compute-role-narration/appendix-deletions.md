# Appendix: deletions and their followers (read 2026-11-27, `93b352db6`)

Verdicts come from a read of each candidate; line numbers are of that day and drift. "Synthetic"
means a test writes a file of that name into a temporary repository and does not read the real one.

## Verdicts

| File | Lines | Verdict | Group |
|---|---|---|---|
| `docs/runbooks/qhh-continuous.md` | 486 | delete | 5.1 |
| `docs/runbooks/qhh-mvp-smoke-evidence.md` | 56 | delete | 5.1 |
| `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` | 1197 | delete | 5.1 |
| `docs/runbooks/qhh-controlled-failure-retry-evidence.md` | 94 | delete | 5.1 |
| `docs/runbooks/two-node-production-e2e-plan.md` | 893 | delete, with named successors | 5.2 |
| `docs/runbooks/node22-db-retirement-runbook.md` | 151 | delete | 5.2 |
| `docs/runbooks/qhh-backend-smoke.md` | 254 | delete after moving about 12 lines | 6.1 |
| `docs/runbooks/qhh-22-business-bringup.md` | 510 | delete; residues listed below | 6.2 |
| `docs/runbooks/forcing-copyback-backfill.md` | 203 | keep: the CLI is live code under test and `forcing-copyback-backfill` requires the operator documentation | 4.4 (narration and header) |
| `docs/runbooks/two-node-deployment-overview.md` | | keep: authority for the env allowlist and read by tests | 4.5 |
| `docs/runbooks/display-readonly-live-mvt.md` | | keep | 4.4 |
| `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md` | | keep; rows that name deleted files are edited with them | 5.1, 6.2 |

## Followers per deleted file

**`qhh-continuous.md`**: the entry in the section-marker list of `tests/test_production_scheduler.py`
(the test keeps `docs/VALIDATION.md`, whose `--plan` / dry-run tokens satisfy its assert);
`LEGACY_DEAD_CODE_INVENTORY.md` rows; a link from `qhh-mvp-smoke-evidence.md` (deleted with it).

**`qhh-mvp-production-like-e2e-checklist.md`**: the second entry of the same list; the requirement of
`evidence-boundary-hardening` "Historical MVP runbooks are clearly marked" (removed);
`docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` rows.

**`qhh-mvp-smoke-evidence.md`**: rows in both governance inventories; links from `qhh-backend-smoke.md`
and `qhh-continuous.md`.

**`qhh-controlled-failure-retry-evidence.md`**: links from `qhh-mvp-smoke-evidence.md` only.

**`two-node-production-e2e-plan.md`**:

- `tests/test_readonly_db_validation_routes.py`: one test's subject is the plan's snippet (check mode
  0600, then `source`, then the validator). The invariant is real. The snippet moves to
  `infra/env/README.md`, which already states the rule and is already read by the same file's other
  test.
- `openspec/specs/evidence-boundary-hardening/spec.md`: one scenario names the plan.
- Pointer sentences that name the plan as the authority for the current display database and env:
  `display-readonly-live-mvt.md` (two), `object-store-forcing-series-read.md` (one). Retargeted to
  `infra/env/display.example`, `docs/governance/ROLE_BOUNDARY.md` and
  `docs/runbooks/node-27-bringup-checklist.md`. These are rewrites of a sentence, not link cuts.
- Links in `two-node-deployment-overview.md` (two) and `API_CONTRACT_RETIREMENT_INVENTORY.md` (two).
- A synthetic path in `tests/test_entropy_audit_topology_authority.py` (no change needed).
- Content that exists only in the plan and is not re-homed: the prose on the readonly validator's
  merge semantics. The behaviour is enforced by the validator; live usage examples are in
  `tier-node27-timeseries-storage.md` and the node-27 checklist.

**`node22-db-retirement-runbook.md`**: `tests/test_role_boundary_static.py` (the file constant and the
rollback-cleanup snippet test; the snippet survives on the 2026-06-29 receipt, which is history);
two synthetic paths in `tests/test_entropy_audit_topology_db_boundary.py`.

**`qhh-backend-smoke.md`**:

- `scripts/diagnostic/qhh/README.md` already carries the detached-root recipe and the
  exact-interpreter rule for all four entrypoints. The unique operator text (the local database helper
  commands, `QHH_CYCLE_TIME`, `QHH_RESET_SMOKE_DB=1`, `SHUD_TIMEOUT_SECONDS`, the script defaults)
  moves there.
- `tests/test_qhh_scripts_static.py`: the test that reads the runbook's recipe is re-pointed at the
  README.
- The freshness anchor in the governance schema and the line of `apps/api/AGENTS.md` that names the
  runbook.

**`qhh-22-business-bringup.md`**:

- Covered elsewhere and not re-homed: the evidence-retention timer (tracked unit, drift probe,
  `scheduler-evidence-retention` spec, the manual-recovery runbook), the journal narrative
  (`pipeline-job-persistence`, `file-orchestration-journal`), the monitoring snapshot catalogue
  (`production-ops-readiness`).
- `tests/test_node22_entrypoint_invariant.py`: the test whose subject is the runbook's front matter
  goes.
- `tests/test_qhh_entrypoint_authority_invariant.py`: forbids a script header from naming the runbook
  as its authority. Becomes: a script header names no deleted runbook.
- Header comments of `scripts/run_qhh_continuous.py` and `scripts/run_qhh_cycle.sh`;
  `tests/fixtures/basins/qhh-sample/README.md`.
- Pointers in `current-production-ops.md` (two) and `production-ops/stuck-detection.md` (one).
- Three citations of its quantization row in `docs/forcing数据处理流程与rSHUD一致性说明.md`, reworded.
- A row of `LEGACY_DEAD_CODE_INVENTORY.md`.

## Outside this change (reported to the owner)

`docs/runbooks/feat-reach-geom-oq-findings.md`, `docs/runbooks/reach-geom-rollout-receipt.md` and
`docs/governance/GOVERNANCE_8_FINAL_EVIDENCE_MAP.md` are historical with no live inbound reference
and do not concern the compute role.
