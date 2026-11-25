# Tasks

Evidence floor: the issue's `Verification:` commands, the whole-word grep
loop, and a red run for each B test.

## 1. Spec and docs (orchestrator)

- [ ] 1.1 `frontend-lint-and-layering` delta: the normalizer requirement
  names one masking wrapper.
- [ ] 1.2 Governance docs (`docs/governance/API_CONTRACT_*.md`): the two
  `rg` command lines drop `loadHydroMetBootstrap`; the sentences about the
  full-bootstrap call are rewritten; every line reference into
  `stores/hydroMetBootstrap.ts` and `lib/hydroMet/riverForecast.ts` is
  re-read from the final files (both files lose lines above the calls
  cited).

## 2. Delete dead code (implementer)

- [ ] 2.1 A1: first move into direct tests, and run green, the assertions
  of the block that cover live functions: the `sanitizeHydroMetMessage`
  redaction (same inputs; expected strings that are sanitize output — the
  `NO_READY_PRODUCT` / `ERR_QHH` reasons come from
  `productAvailabilityReasons` and are not carried over), and
  `fetchHydroMetStationsByIdentity` (the query it sends, the runtime
  coordinate normalisation of the page it returns, the failure path). Then
  delete the full-bootstrap path, its closure and its `describe` block.
  Unused imports and test helpers that only the block used go too.
- [ ] 2.2 A2 (after 3.1 is green, so the cap test guards the rewrite):
  delete the point-budget status branch and the guard (D2); the call site
  in `lib/hydroMet/riverForecast.ts` becomes a slice with the same result
  for every input; remove the two over-budget chart cases.
- [ ] 2.3 A3: delete `ForecastData.source`, `ForecastSeries.role` and the
  legacy colour members; switch the TS fixtures to live colours; update
  the two Python source-text assertions (D4).
- [ ] 2.4 A4: delete `hydroMetStationFilterAvailability`,
  `riverForecastRequestKey` and its test, `ForecastSeries.isAnalysis` and
  the issue-time divider (D3: the divider mark line, the `showIssueDivider`
  condition and its use in `ForecastChart.tsx`), with the lines that set
  the field (the panel's `isAnalysis: false` and the test fixtures).

## 3. Tests for live behaviour (implementer; no production change)

- [ ] 3.1 B1 (written first): a forecast response with more than 480
  points yields exactly 480 rendered points and `capped: true` (the
  point count reported before the cap stays the full count); one with
  exactly 480 yields 480 and `capped: false`. The cap applies before the
  sort, so the input is in ascending time order.
- [ ] 3.2 B2: a response with more than 8 series fails validation with the
  inspection-cap message.
- [ ] 3.3 B3: a discharge click on a feature without `basin_version_id`,
  and one without `river_network_version_id`, opens no river popup; the
  same click with full identity does (in the same test file, so the
  negative cases are not vacuous).
- [ ] 3.4 B4: two concurrent same-key `fetchHydroMetLatestProduct` calls
  issue one GET and resolve to the same product; after a rejected request
  the next call issues a new GET.

## 4. Evidence

- [ ] 4.1 Red runs for 3.1-3.4: each new case fails when the line it
  covers is reverted or removed locally (the cap, the inspection limit,
  each half of the click guard, the pending-promise reuse, the cache
  eviction on rejection), then the source is restored and compared
  byte-identical. For the inspection limit the line to remove is the
  validation message; the slice next to it shows no effect once validation
  has failed. Recorded in `.workplans/3241/red-runs.log`.
- [ ] 4.2 The issue's whole-word grep loop, plus `isAnalysis` and
  `createForecastPointBudgetGuard`, each run as
  `git grep -n -w <symbol> -- . ':!openspec/changes' ':!**/__tests__/**'
  ':!**/*.test.*'`, prints nothing (`settledError` in
  `openspec/specs/frontend-lint-and-layering/spec.md` goes at archive).
- [ ] 4.3 `cd apps/frontend && pnpm typecheck && pnpm lint && pnpm test &&
  pnpm build`; `pnpm check:api-types`; from the repo root the two Python
  tests named in the issue; `ruff check --no-cache .`; the entropy hard
  gate at the baseline counts; `openspec validate --strict`.
