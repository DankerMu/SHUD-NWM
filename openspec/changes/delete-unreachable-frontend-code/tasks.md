# Tasks

## 1. Delete

- [ ] 1.1 Delete the eight unreachable files listed in proposal.md.
- [ ] 1.2 Move `ForecastData`, `ForecastSeries`, `ForecastSeriesPoint` unchanged to `src/lib/forecastData.ts`; switch
      the importers (`components/charts/ForecastChart.tsx`, `components/map/M11RiverForecastPanel.tsx`,
      `components/charts/__tests__/ForecastChartFillResize.test.tsx`, `ForecastChartZoomConfig.test.tsx`); delete
      `src/stores/forecast.ts`.
- [ ] 1.3 Remove directories left empty (`src/components/forecast/`).
- [ ] 1.4 Remove `@radix-ui/react-dropdown-menu` and `@radix-ui/react-tabs` from `apps/frontend/package.json` and
      regenerate `pnpm-lock.yaml` (frontend lock only; verify no other importer first).
- [ ] 1.5 D8: delete `takeSeries` from `lib/forecastRenderingBudget.ts` if no non-test caller remains (it has no direct
      test; the store cases that exercised it are deleted in 2.1). Leave `loadHydroMetBootstrap` and
      `isForecastPointOverBudget` alone (the latter was already callerless before this change: noted, not deleted).

## 2. Tests (per case, D3)

- [ ] 2.1 `src/__tests__/ForecastComparison.test.tsx`: re-home the five `ForecastChart` render cases to
      `src/components/charts/__tests__/ForecastChartComparison.test.tsx`; delete the ten store / selector cases and
      the `ScenarioSelector` render case.
- [ ] 2.2 Delete `src/pages/m11/__tests__/useHydroMetProduct.test.tsx` (fixture review: cases 2 and 3 test only the
      deleted hook/store; case 1 goes through the live bootstrap, already covered by `bootstrap.test.ts`; case 4 is
      the hook store's own dedupe, the live dedupe is pinned at `bootstrap.test.ts:540`). Re-verify each claim and
      record it in the ledger.
- [ ] 2.3 Python source-text tests per D3: `tests/test_e2e.py` (~:700), `tests/test_e2e_ifs.py` (~:180);
      `tests/test_forecast_store_routing.py:442` docstring rewritten to be true.
- [ ] 2.4 `deleted-tests.md` in this change: one row per deleted or re-homed test case (file, name, what it asserted,
      disposition, where the behaviour is pinned now or why it is dead), plus the D9 gaps.

## 3. Docs and spec

- [ ] 3.1 `docs/governance/API_CONTRACT_FRONTEND_CONSUMER_EVIDENCE.md`, `API_CONTRACT_OPENAPI_TYPE_SYNC_EVIDENCE.md`,
      `API_CONTRACT_REMOVAL_DEFERRAL.md`, `API_CONTRACT_RETIREMENT_INVENTORY.md`,
      `LEGACY_DEAD_CODE_INVENTORY.md`: per D5 (no deleted path cited as a live consumer; deleted-test evidence
      replaced by the live test; line references corrected for the files this change touches).
- [ ] 3.2 Spec deltas for four capabilities (orchestrator; D6).

## Evidence Floor

- E1 `cd apps/frontend && corepack pnpm typecheck && corepack pnpm lint && corepack pnpm test && corepack pnpm build
     && corepack pnpm check:api-types` (locally `pnpm` if corepack is not set up).
- E2 `uv run python artifacts/issue-3222/reach.py apps/frontend`: "unreachable from everything" is empty and the
     lanes-only list holds only `src/lib/c4DisplayEvidence/*` and `src/lib/riverClickEvidence/*`.
- E3 this command prints nothing:
     `git grep -n -P "stores/forecast\b|useForecastStore|\bForecastPanel\b|components/forecast/|ScenarioSelector|\bSegmentInfo\b|ForecastSegmentInfo|usePagination|ui/tabs|ui/dropdown-menu|react-dropdown-menu|react-tabs|useHydroMetProduct\b|hydroMetProductData|Scenario 选择器|DropdownMenu"
     -- apps/frontend tests docs openspec/specs ':!docs/archive' ':!docs/review-loop-log.jsonl'`
     (`-P`, not `-E`: on macOS `git grep -E` has no `\b`; `\bForecastPanel\b` does not match `M11RiverForecastPanel`; `openspec/changes/**` is history and outside the
     pathspec; after the archive the four specs no longer carry these names).
- E4 The vitest test count before and after is recorded in the PR; the delta equals the deleted cases in the ledger
     (re-homed cases keep their count).
- E5 `uv run ruff check . --no-cache`; `uv run pytest -q tests/test_e2e.py tests/test_e2e_ifs.py
     tests/test_forecast_store_routing.py` plus any test that scans the governance docs or specs touched.
- E6 `openspec validate delete-unreachable-frontend-code --strict --no-interactive`; after archive each touched spec
     validates on its own.
- E7 CI: Frontend Build, every "Frontend E2E (mocked)" shard, every "Unit Tests (full)" shard.
- E8 node-27 after merge: `dist` rebuilt and deployed, `/` and `/ops` load locally and on the public entry, the river
     popup chart renders for one segment (browser check). The bound C4 receipt needs operator-approved pins and is
     reported as not run if they are not available.
