# Delete the M22 E2E plan and the DB retirement runbook (#3406)

Slice 5.2 of `openspec/changes/sweep-compute-role-narration` (#3345). Fixture level: compact
(deletions whose tests are re-homed, not dropped; no runtime behaviour). Risk packs: Documentation /
migration notes - selected (two documents leave the tree; a snippet that tests pin gets a new home;
pointer sentences are retargeted). Legacy compatibility / examples - selected (three tests keep
proving what they proved). Others - not selected.

## Why

The owner decided on 2026-11-27 that runbooks which are history only are deleted, not rewritten.

- `docs/runbooks/two-node-production-e2e-plan.md` (893 lines) carries the notice "historical /
  superseded M22 evidence plan". It is still the only place with the full shell snippet "create the
  secret-source file with mode 0600, check the mode, then source it, then run the readonly DB
  validator with its evidence arguments", which two tests read there, and three sentences in live
  runbooks still name it as an authority for the display environment.
- `docs/runbooks/node22-db-retirement-runbook.md` (151 lines) records a stop gate that was completed
  on 2026-06-29. Its receipt
  (`docs/runbooks/receipts/2026-06-29-node22-db-retirement-stop.md`) is the authoritative record and
  stays.

## What Changes

- Both files are deleted (git keeps them).
- **The snippet moves to `infra/env/README.md`**, next to the paragraph that already states the
  fail-closed rule for local secret-source files. The whole block moves: the evidence-root split,
  the secret-source guard, the per-source validator loop and the merge command. Its comments are
  written in English like the page; no command changes.
- `tests/test_readonly_db_validation_routes.py`: the two tests that read the plan read the README.
  Every assertion stays (the evidence arguments and the absence of a doubled run-id directory; the
  order create < source, mode check < source, source < validator; the fail-closed forms and the
  absence of the two weak forms).
- `tests/test_role_boundary_static.py`: the rollback-cleanup snippet test walks the receipt only; the
  constant for the runbook goes.
- `evidence-boundary-hardening`: the requirement "Current runbooks follow M26 route authority" has a
  scenario about the plan. A MODIFIED block cannot drop a scenario, so the requirement is removed
  and added back as "Current runbooks follow the single-map route authority" with its body and its
  three other scenarios unchanged. The rule of the dropped scenario is the one the scenario about
  the node-27 checklist already requires of that checklist.
- Pointer sentences retargeted to `docs/runbooks/node-27-bringup-checklist.md`:
  `display-readonly-live-mvt.md` (two), `object-store-forcing-series-read.md` (one),
  `two-node-deployment-overview.md` (two links).
- `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md`: the two mentions of the plan go.

## Not in this change

- The plan's prose on the validator's merge semantics is not re-homed: the validator enforces it,
  and live usage is shown in `tier-node27-timeseries-storage.md` and the node-27 checklist.
- History keeps its mentions (`docs/plans/`, `docs/bugs.md`, `progress.md`, receipts, archived
  changes). Synthetic paths of the same names in the topology gate's tests are test inputs.
- `node22-db-retirement-cutover` (the capability spec) is not touched: it does not name the runbook.

## Impact

No code. Two test files edited. `infra/env/README.md` gains one section. One requirement of
`evidence-boundary-hardening` is renamed by remove-and-add.
