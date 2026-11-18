# c4-role-selector-negative-test

Issue: #3211
Fixture level: compact
Risk packs: test-evidence
Evidence floor: E1-E3 below

## Why

`apps/frontend/playwright.c4-display-lane.ts:523-525` fails C4 with `FORBIDDEN_CONTROL` /
`role selector is visible on display_readonly /ops` when `observation.roleSelectorCount !== 0`. No test drives that branch:
deleting it leaves every vitest green, so a display_readonly `/ops` showing a role selector could yield a C4 PASS.
The sibling retry/cancel branch got its negative test in #3202 (PR #3208); this one did not.

## What

1. Add one test named `fails FORBIDDEN_CONTROL when /ops shows a role selector` to
   `apps/frontend/src/__tests__/c4DisplayLane.test.tsx`, next to
   `fails FORBIDDEN_CONTROL when /ops shows a retry or cancel control`. Reuse the same full PASS fixture
   (`homeResponses()`, `opsResponses`, real `logResponses`); override only `opsObservation` with
   `headingObserved: true, roleSelectorCount: 1` (retry/cancel count stays 0).
2. Assert `result.ok === false` and `result.terminal.failure` toEqual
   `{code: 'FORBIDDEN_CONTROL', stage: 'ops', message: 'role selector is visible on display_readonly /ops'}`.
3. Spec delta: one added scenario on the C4 bounded-readonly requirement.

## Must preserve

- `apps/frontend/playwright.c4-display-lane.ts` is byte-identical to master.
- All existing c4DisplayLane tests keep passing.

## Out of scope

Any production code change; the C4 runtime/observer.

## Evidence Floor

- [x] E1 Red proof: temporarily delete the branch at `playwright.c4-display-lane.ts:523-525`, run
  `corepack pnpm vitest run src/__tests__/c4DisplayLane.test.tsx -t 'role selector'` → 1 failed; restore, then
  `git diff --exit-code master -- apps/frontend/playwright.c4-display-lane.ts`.
- [x] E2 `cd apps/frontend && corepack pnpm test && corepack pnpm check:types && corepack pnpm typecheck`.
- [ ] E3 `openspec validate c4-role-selector-negative-test --strict --no-interactive`. No node-27 run (test-only, no
  runtime change).
