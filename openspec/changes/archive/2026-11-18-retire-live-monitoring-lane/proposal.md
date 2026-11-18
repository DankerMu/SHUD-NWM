# Proposal — retire-live-monitoring-lane (#3202)

Closes #3202.

```text
Issue type: retirement (dead evidence lane)
Fixture level: standard
Upstream suggested level: absent
Blast radius: frontend live Playwright lanes (river-click and C4 must stay intact); frontend contract tests; docs; four specs
Selected risk packs: Legacy compatibility, CI / environment
Evidence floor: E1-E5 below
```

## Why

Owner decision (2026-11-18): **retire** the legacy `@live-monitoring` live-display lane, rather than retarget it to
`/ops`.

The lane visits `/monitoring` and requires `监控工作台` with no `权限不足`. `/monitoring` is role-gated without
`allowDisplayReadonly` (`apps/frontend/src/App.tsx:72-79`; spec `ops-display-downgrade` forbids widening it), and the
public deployment is a viewer build with `service_role=display_readonly`. So the lane can never PASS. It failed
identically on Playwright 1.59 and 1.64 (#3198 E4).

The C4 lane (`test:e2e:live-c4-display`) is the live `display_readonly` browser evidence for `/ops`. It owns its own
negative contracts: the verdict lives in `playwright.c4-display-lane.ts:516-528`; `dom.ts` only observes.

Tested today, in `src/__tests__/c4DisplayLane.test.tsx`:
- 权限不足 (`:269`, `PERMISSION_DENIED`);
- slurm control (`:283`, `FORBIDDEN_CONTROL`);
- non-GET requests (`:712`).

**Not yet tested** and added by this change, because the deleted `playwrightConfig.test.ts` legs covered the first:
- `/ops` runtime config unavailable (`:519`);
- visible retry/cancel controls (`:526`).

## What

1. **Delete:**
   - `apps/frontend/playwright.live-display.config.ts` and `apps/frontend/scripts/playwright-live-display.sh`;
   - the `package.json` `test:e2e:live-display` script and the `tsconfig.node-playwright.json` include for the deleted
     config;
   - the `@live-monitoring` test in `apps/frontend/e2e/live-display.spec.ts` (`:27-88`), its 8 helper imports
     (`:4-13`), and the now-unused `expect` import (`:2`). `noUnusedLocals` would otherwise fail `check:types`. Keep `test`
     and the `:1` fs import that river-click uses.
2. **Helpers used only by that lane:** delete them from `apps/frontend/playwright.config.helpers.ts`:
   - `monitoringReadApiPaths`, `liveDisplayApiBinding`, `isLiveDisplayRuntimeConfigUrl`, `isLiveDisplayReadApiUrl`;
   - `classifyLiveDisplayControlRequest`, `assertLiveDisplayPageEvidence`, `createLiveDisplayReadApiEvidence`;
   - `parseLiveDisplayRuntimeConfigEvidence`, `runtimeConfigParseFailure`, `parseBoundedContentLength`,
     `liveDisplayRuntimeConfigMaxBytes`;
   - the types `LiveDisplayApiBindingMode`, `LiveDisplayApiBinding`, `LiveDisplayBrowserResponse`,
     `LiveDisplayPageEvidence` and `LiveDisplayResponseSource`.
3. **Keep, because river-click or C4 use them:**
   - `isDisplayReadonlyRuntimeConfig`, with `unwrapApiData` and `LiveDisplayRuntimeConfig`, which it depends on.
     `playwright.c4-display-lane.ts:38` imports it and calls it at `:447` and `:458`;
   - `LiveDisplayEnv`, `normalizeHttpUrl`;
   - `loadLiveDisplayEnv`, `liveDisplaySpecPattern`, `assertLiveDisplaySpecsDoNotMockApis`;
   - the spec finders, the broad and any-route mock guards, the C4 spec guards, `parsePlaywrightWorkers`;
   - the `e2e/live-display.spec.ts` file itself, which holds `@live-river-click`;
   - the `playwright.config.ts` mocked-lane exclusion of `live-display.spec.ts`.
4. **Frontend contract tests:**

   | Test | Disposition |
   |---|---|
   | `playwrightConfig.test.ts` | delete the 6 `@live-monitoring` helper `it` blocks (`:167-383`) and trim the imports; keep the workers, env, spec-match, broad-mock and symlink guards |
   | `c4LiveDisplayContract.test.ts` | `:12-13` becomes `toBeUndefined()`; delete the `:17-20` `readFileSync(playwright.live-display.config.ts)` block, the monitoring part of the `:57` `it` title and its `:66-68` import, and the `:133-135` monitoring list; keep `not.toMatch(/@live-monitoring/)` as a regression guard |
   | `riverClickLiveDisplayContract.test.ts` | `:16-17` becomes `toBeUndefined()`; delete the `:19-21` `readFileSync`; delete the monitoring-only-config `it` (`:53-72`) |
   | `c4DisplayLane.test.tsx` | **add** two `it` blocks: `opsObservation: {...defaultC4OpsObservation(), runtimeUnavailable: true}` gives `OPS_UNAVAILABLE`, and `{..., headingObserved: true, retryCancelControlCount: 1}` gives `FORBIDDEN_CONTROL` |
   | `riverClickEvidenceOwner.test.ts` | delete the monitoring-profile `--list` `it` (`:457-503`); fix the `:236` comment to name `playwright.live-river-click.config.ts` |
   | `riverClickTypecheck.test.ts` | delete the `:59` include assertion |
   | `runbookContract.test.ts` | unchanged: its `not.toContain('test:e2e:live-display')` still holds |
5. **Docs:**
   - `docs/VALIDATION.md:584-616`:
     - remove "Only this lane may produce live `display_readonly` receipts";
     - add one bullet each for `live-c4-display` and `live-river-click`, saying whether API mocks are allowed and which
       receipt the lane can produce. The evidence-boundary-hardening scenario requires this;
     - add the C4 command block with its five environment keys;
     - reword the BLOCKED description. The kept configs replace a missing URL with a `127.0.0.1:0` placeholder, and their
       globalSetup owner emits a BLOCKED receipt; they do not exit before the browser runs.
   - `docs/runbooks/two-node-production-e2e-plan.md:699-701`: use the C4 command and its environment keys.
   - `docs/runbooks/node-27-bringup-checklist.md:442`.
   - `docs/runbooks/tier-node27-timeseries-storage.md:2921`, `:3118`: say the lane was retired.
   - `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md:272-276`: the guard now runs on the river-click config.
   - `docs/bugs.md:363-375`, `:442-450`: update the evidence paths, replace the retest with the C4 `/ops` check, and add
     the note "lane retired by #3202".
   - `apps/frontend/AGENTS.md:57-63` is hand-written and scoped. Keep every reference and command string that
     `scripts/governance/entropy_audit/schema.py:279-299` pins.
6. **Specs:** MODIFIED wording, with no scenario dropped:
   - `display-route-evidence-cleanup` and `evidence-boundary-hardening`: the WHEN/THEN lines name the C4 and river-click
     lanes;
   - `frontend-river-click-live-evidence`: the Requirement body stops requiring the `/monitoring` test to remain, and
     records the retirement;
   - `frontend-testing`: names the live lanes.

## Non-goals

- `/monitoring` and `/system/model-assets` RBAC: no `App.tsx` change.
- `@live-river-click`, C4, the mocked lane.
- Renaming the `live-display.spec.ts` / `playwright.live-display.global-setup.ts` files, which keep their historical
  names.
- C4 lane behaviour. Adding the two C4 tests above changes no C4 code.

## Must preserve

- river-click and C4 configs, specs and their guards behave identically;
- the no-broad-mock guard still covers `live-display.spec.ts`;
- the negative live contracts remain enforced and tested on the C4 path.

## Evidence Floor

- [x] E1 `cd apps/frontend && corepack pnpm test && corepack pnpm check:types && corepack pnpm typecheck && corepack pnpm build`;
  `uv run ruff check --no-cache .`; `git diff --check`.
- [x] E2 The grep below may hit only three kinds of line, each listed in the PR:
  - regression-guard assertions (`not.toContain`, `not.toMatch`, `toBeUndefined`);
  - dated historical receipts;
  - the What 5 "retired by #3202" notes.

  `openspec/specs` is clean only after the archive.

  ```
  git grep -nE "test:e2e:live-display([^-]|$)|playwright\.live-display\.config|playwright-live-display\.sh|@live-monitoring|monitoringReadApiPaths|assertLiveDisplayPageEvidence|liveDisplayApiBinding|\`live-display\`|live-display-readonly" -- apps docs instructions AGENTS.md CLAUDE.md scripts tests .github
  ```
- [x] E3 Each command lists its own tag and never `@live-monitoring`. This duplicates
  `c4LiveDisplayContract.test.ts:86-143` inside E1. With the documented environment keys set:
  - `cd apps/frontend && corepack pnpm exec playwright test --config playwright.live-river-click.config.ts --list`
    lists `@live-river-click`;
  - the same command with `--config playwright.live-c4-display.config.ts` lists `@live-c4-display`.
- [x] E4 `git diff master -- apps/frontend/src/App.tsx` is empty.
- [x] E5 `openspec validate retire-live-monitoring-lane --strict --no-interactive`. The PR records:
  - the C4 negative-contract coverage: the 3 existing tests plus the 2 added ones;
  - that no node-27 run is needed, because a lane is being retired.
