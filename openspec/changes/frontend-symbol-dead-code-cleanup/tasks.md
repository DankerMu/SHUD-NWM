# Tasks

Evidence floor: the issue's `Verification:` commands, the whole-word grep
loop, and a red run for each B test.

## 1. Spec and docs (orchestrator)

- [ ] 1.1 `frontend-lint-and-layering` delta: the normalizer requirement
  names one masking wrapper.
- [ ] 1.2 Governance docs: the two `rg` command lines drop
  `loadHydroMetBootstrap`; the sentences and line references about the
  full-bootstrap call are brought in line with the tree after the deletion
  (line numbers re-read from the final file).

## 2. Delete dead code (implementer)

- [ ] 2.1 A1: move the `sanitizeHydroMetMessage` redaction assertions into
  a direct test first (same inputs, same expected strings), run it green,
  then delete the full-bootstrap path, its closure and its `describe`
  block. Unused imports and test helpers that only the block used go too.
- [ ] 2.2 A2: delete the point-budget status branch and the guard (D2);
  the call site in `lib/hydroMet/riverForecast.ts` becomes a slice with the
  same result for every input; remove the two over-budget chart cases.
- [ ] 2.3 A3: delete `ForecastData.source`, `ForecastSeries.role` and the
  legacy colour members; switch the TS fixtures to live colours; update
  the two Python source-text assertions (D4).
- [ ] 2.4 A4: delete `hydroMetStationFilterAvailability`,
  `riverForecastRequestKey` and its test, `ForecastSeries.isAnalysis` and
  the issue-time divider (D3), with the fixture lines that set the field.

## 3. Tests for live behaviour (implementer; no production change)

- [ ] 3.1 B1: a forecast response with more than 480 points yields exactly
  480 points and `capped: true`; one with exactly 480 yields 480 and
  `capped: false`.
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
  byte-identical. Recorded in `.workplans/3241/red-runs.log`.
- [ ] 4.2 The issue's whole-word grep loop prints nothing outside tests
  and archives; `git grep -n -w isAnalysis` and
  `git grep -n -w createForecastPointBudgetGuard` print nothing.
- [ ] 4.3 `cd apps/frontend && pnpm typecheck && pnpm lint && pnpm test &&
  pnpm build`; `pnpm check:api-types`; from the repo root the two Python
  tests named in the issue; `ruff check --no-cache .`; the entropy hard
  gate at the baseline counts; `openspec validate --strict`.
