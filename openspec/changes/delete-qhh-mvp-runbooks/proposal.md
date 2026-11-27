# Delete the four QHH MVP evidence runbooks (#3405)

Slice 5.1 of `openspec/changes/sweep-compute-role-narration` (#3345). Fixture level: compact
(deletions with followers; no runtime behaviour). Risk packs: Documentation / migration notes -
selected (four documents leave the tree; every reference follows). Legacy compatibility / examples -
selected (one test loses two of its three carriers and must keep proving its point). Others - not
selected.

## Why

The owner decided on 2026-11-27 that runbooks which are history only are deleted, not rewritten
(`sweep-compute-role-narration`, proposal). Four runbooks belong to the QHH MVP evidence freeze
(#213, #214); none is the authority for anything live, and git keeps them:

- `docs/runbooks/qhh-continuous.md`
- `docs/runbooks/qhh-mvp-smoke-evidence.md`
- `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md`
- `docs/runbooks/qhh-controlled-failure-retry-evidence.md`

The first three carry a historical notice or are a frozen evidence matrix. The fourth has no notice
and was last edited for #3179: it is an index of deterministic evidence for one scenario
(`ops-mvp-pipeline-control`, "Controlled failure evidence"). What it points at stays: the rule is
that scenario, the evidence is
`tests/test_monitoring_api.py::test_qhh_like_controlled_failure_retry_evidence_propagates_one_formal_identity`
and the case "proves the /ops controlled failure log and marker-lane recovery lifecycle" of
`apps/frontend/e2e/monitoring.mocked.spec.ts`, and the recovery steps are in
`docs/runbooks/node22-control-plane-manual-recovery.md`. Its remaining text is the #214 evidence
boundary, which goes with the matrix it refers to.

## What Changes

- The four files are deleted.
- `tests/test_production_scheduler.py`,
  `test_docs_reserve_plan_for_no_mutation_and_use_submit_for_production_submission`: the two
  carriers that are deleted leave its list. `docs/VALIDATION.md` stays and carries both halves of
  the assertion (the production section uses `--submit` and never `--plan`; the page reserves
  `--plan` for the dry-run / no-mutation path).
- `evidence-boundary-hardening`: the requirement "Historical MVP runbooks are clearly marked" is
  removed. Its one scenario is about the checklist that is deleted. The old runbooks that remain for
  now (`qhh-backend-smoke.md`, `two-node-production-e2e-plan.md`) keep their notices and are deleted
  by #3406 and #3408; the route-authority requirement of the same capability still classifies every
  old display path in a current runbook.
- `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` and
  `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`: the mentions of these four files go; rows and
  bullets that also name files of later slices are edited only for these four names.
- `docs/runbooks/qhh-backend-smoke.md`: its one pointer to `qhh-mvp-smoke-evidence.md` goes (the file
  itself is #3408's).

## Not in this change

- History keeps its mentions: `docs/plans/`, `docs/bugs.md`, `progress.md`,
  `docs/review-loop-log.jsonl`, receipts, archived changes.
- The run-root directory name `qhh-continuous` in scripts and specs is a name, not the runbook.
- `qhh-backend-smoke.md`, `qhh-22-business-bringup.md` (group 6) and the two files of #3406.

## Impact

No code. One test edited. One requirement removed from `evidence-boundary-hardening`.
