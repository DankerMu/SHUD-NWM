# Tasks: the recovery hint on `/ops` names the compute control host (#3424)

## Must preserve

- The element ids `ops-manual-recovery-guidance` and `ops-stage-manual-recovery-guidance`;
  `operatorRecoveryVisible`; the receipt key `operator_recovery_visible`.
- The probe's order: the element first, the text second.
- Every other sentence of the hints, and when each variant is shown.
- `apps/frontend/src/api/types.ts`, `package.json`, `pnpm-lock.yaml`: untouched.
- Nothing is built for deployment, swapped or restarted.

## 1. Frontend

- [x] 1.1 `JobsTable.tsx`, `BasinFailures.tsx`: the four hint variants (`design.md` D1).
- [x] 1.2 The assertions (D1): `JobsTable.test.tsx`, the two of the other variant expect
  "计算控制主机", the one of the `display_readonly` variant expects "计算控制主机" and
  "node-27 display_readonly"; `StageCard.test.tsx`, the existing one expects "计算控制主机" and a new
  case with `diagnosticsDisplayReadonly` expects "计算控制主机" and "node-27 display_readonly".
- [x] 1.3 `src/lib/c4DisplayEvidence/dom.ts`: the fallback text; `src/test/c4DisplayFakePage.ts`:
  the sentence (D1).
- [x] 1.4 Three new unit tests of `inspectC4OpsInPage` in `src/__tests__/c4DisplayLane.test.tsx`, one
  per case (D2).
- [x] 1.5 The four comment lines (D1).

## 2. Spec

- [x] 2.1 `display-readonly-ops-ui`: two MODIFIED blocks (D3).
- [x] 2.2 The change is archived in the PR's last push: the two requirements in the main spec equal
  the generated blocks.
- [x] 2.3 Task 8.2 ticked in `openspec/changes/sweep-compute-role-narration/tasks.md`.

## Evidence Floor

- Red runs, each logged with its command and the failing assertion:
  - R1a: the five assertions of 1.2 written (they sit in four tests: in `JobsTable.test.tsx` one
    test holds an assertion of each variant), the two components not yet changed: the four tests
    fail on the old text.
  - R1b: only the other variant of `JobsTable.tsx` changed: that test now passes its first
    assertion and fails on the `display_readonly` one ("node-27 display_readonly"), which R1a
    could not reach.
  - R2: the three new probe tests written, `dom.ts` not yet changed: the test of the new fallback
    text fails, the two other tests pass.
- Green: `pnpm test` (the whole unit suite), `pnpm typecheck`, `pnpm lint`, `pnpm check:api-types`
  (no diff: the types are not touched); the mocked Playwright lane: locally
  `e2e/monitoring.mocked.spec.ts` (the spec file whose comments change), in CI every shard.
- The four rendered hints, read from the DOM in the tests (or printed from them), and the fake
  page's sentence.
- `git diff --stat`: the files of section 1 and nothing else under `apps/frontend`. Two of them are
  outside the PR boundary the issue lists and are recorded in the PR: `src/__tests__/c4DisplayLane.test.tsx`
  (the probe's unit test, which the acceptance criteria ask for) and `e2e/monitoring.mocked.spec.ts`
  (two comment lines the issue's scope names).
- Hard gate: the finding multiset (check id, path, line) identical before and after.
- `narration_count.py` plain and `--tokens` before and after; every moved name token explained;
  the bare-22 shorthand count.
- `openspec validate ops-hint-names-compute-control-host --strict --no-interactive`; after the
  archive, the spec and `openspec validate sweep-compute-role-narration --strict --no-interactive`.
- `uv run ruff check --no-cache .`; markdownlint on the edited documents; CI green on every shard.
- Deviation recorded in the PR: the live receipt of the served page (C4) follows in slice 8.3.

Suggested fixture level: expanded - user-visible text on the production page and the probe of its live receipt.
Minimal mergeable slice: atomic - the components, their five assertions, the probe's fallback and the fake page must move together or the unit suite fails.
