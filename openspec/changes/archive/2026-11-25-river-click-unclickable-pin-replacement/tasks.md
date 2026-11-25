# Tasks

Evidence floor: `openspec validate --strict`; the runbook contract test;
the issue's own alternative, a documented product finding (#3390).

## 1. Spec and runbooks (orchestrator)

- [x] 1.1 `frontend-river-click-live-evidence` delta: the replacement rule
  and its three scenarios.
- [x] 1.2 `docs/runbooks/node-27-bringup-checklist.md`: signature, rule,
  `networks_by_size.tsv`; `docs/runbooks/tier-node27-timeseries-storage.md`
  points at it.

## 2. Contract test (implementer; test file only)

- [x] 2.1 `apps/frontend/src/__tests__/runbookContract.test.ts`: the
  checklist's river-click section states the signature (the failure code,
  the exact message, `warmup_count 0`, `accepted_count 0`), the limit of
  two replacements per role, that any failure after the warmup is NO-GO,
  and that the pin script writes `networks_by_size.tsv`; the tier runbook
  section points at the checklist rule. Each assertion red once against
  the runbook with that sentence removed.

## 3. Evidence

- [x] 3.1 `openspec validate river-click-unclickable-pin-replacement
  --strict --no-interactive`; `cd apps/frontend && pnpm test`; the pin
  script extracted from the runbook runs on a synthetic input and writes
  both files; entropy hard gate at the baseline.
- [x] 3.2 The diagnosis and the product finding are posted on #3242 and
  filed as #3390.
