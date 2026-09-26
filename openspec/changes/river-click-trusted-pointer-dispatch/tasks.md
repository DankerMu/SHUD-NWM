## Risk packs

- **Public API / CLI / script entry: selected.**
  - The gated global changes shape: `selectRenderedRiver` is replaced by `locateRenderedRiver`, `armPointerCapture` and `takePointerCapture`.
  - The lane's five env keys and the `test:e2e:live-river-click` command are unchanged.
  - The binder's accepted version changes.
  - Callers: the lane, the runbooks and the binder. Covered by 2.1-2.4 and 2.7.
- **Schema / field names: selected.** Receipt 1.0 → 1.1, the new required `click_dispatch` constant and the new FAIL code (D3). Covered by 2.3.
- **Legacy compatibility: selected.** A 1.0 document no longer validates or binds; this is deliberate (D3). 1.0 PASS receipts do exist: the hook-dispatch baseline of §1.3, kept in `/home/nwm/tmp/q/receipts/`. They are the "before" only and must never bind. Ordinary users see no change without the flag (2.1 tests).
- **Auth / permissions / secrets: selected.**
  - The hook stays gated.
  - It exposes no map ref and no fetch.
  - The capture records only a timestamp and a client point.
  - The receipt redaction rules are unchanged.

  Covered by 2.1 and 2.3.
- **Release / operational: selected.** The frontend `dist` is deployed on node-27 after merge, followed by three public-site receipts (§5). The pre-merge throwaway stack must not touch production (§4).
- **Documentation: selected.** Checklist C4 ④⑤ and tier §4.9 (2.7).
- **File IO / path safety / overwrite: selected.**
  - The mode-0600 no-clobber publisher and the private 0700 run directory are unchanged; their tests must stay green (2.3).
  - The throwaway stack's private MVT cache directory must never be the production one (4.2).
  - The post-merge `dist` swap (5.1).
- **Error handling / rollback / partial outputs: selected.**
  - The new closed codes `HOOK_POINT_OCCLUDED`, `HOOK_POINTER_MISSING`, `HOOK_POINTER_INVALID` and `CLICK_DISPATCH_INVALID`.
  - Hook-code propagation.
  - Terminal receipts on every failure (2.1-2.3).
  - A bad `dist` is rolled back by swapping back to `dist.old` (5.1).
- **Config / project setup: selected.** The five env keys, the `live-river-click` Playwright profile and its worker/retry/timeout values are unchanged (Must-preserve). The throwaway API's env overrides are listed in 4.2.
- **Resource limits: selected.** The per-attempt 15 s and whole-run 360 s deadlines, the 64-result query cap and the validator caps are unchanged (Must-preserve, 2.1 carried-over tests).
- **Concurrency: selected.** The lane is serial. The pre-merge second API process shares only the read-only DB, with one worker's pool, and uses a private MVT cache (4.2), so it cannot race the production tile tree.
- **Not selected:**
  - DB / migration: none.
  - Performance of production code: the hook is inert without the flag.
  - Backend CI routing: frontend files are not routed through `select_ci_tests.py`, as the existing spec says.

## Must-preserve

- **Without the pre-start flag:** no global, no listener, and identical pointer, hover, popup and request behaviour.
- **The existing mocked and unit suites stay green**, except the tests that encode the retired direct dispatch; those are rewritten.
- **The lane's config grammar is unchanged:**
  - exactly five env keys;
  - overrides are a config FAIL;
  - the preflight requests and bounds;
  - the series matching rules;
  - warmup 1 + 20 samples;
  - the nearest-rank index 18, and `p95_ms < 2000` strict;
  - the per-attempt 15 s and whole-run 360 s deadlines, and the test timeout of 390 s;
  - one worker, zero retries;
  - the panel close and the 250 ms quiet interval.
- **Receipt publication is unchanged:** mode 0600, no-clobber via link, the private 0700 parent, and the validator caps.
- **The live no-mock guard** (`assertLiveDisplaySpecsDoNotMockApis`) and the exact live-spec matcher.
- **The `/monitoring`, `live-c4-display` and mocked lanes are untouched.**
- **Consumers that must be rewritten on purpose, not silently broken:**
  - `src/__tests__/runbookContract.test.ts`. It slices checklist `#### C4-river-click：` → `## 上线判定` and takes the first bash block that contains `test:e2e:live-river-click`. It pins `corepack pnpm@10.11.0 --dir "$REPO_ROOT/apps/frontend" run …` and the `RUN_ROOT=$(mktemp -d "$REPO_ROOT/.nhms-issue1895-riverclick-XXXXXX")` prelude, and runs the binder on the example. The three-receipt recipe keeps these pins, or the test is updated deliberately in the same commit.
  - `src/components/map/__tests__/M11MapLibreSurfaceHook.test.tsx`: it asserts `Object.keys(hook) == ['selectRenderedRiver']`.
  - `src/test/riverClickFakePage.ts`: it needs `mouse.click`.
  - `playwright.river-click-lane.ts:201`: its readiness probe checks for `selectRenderedRiver`.
  - The C4 tests that use the river example and binder as negative cases: `c4ReceiptBinderCore.test.ts`, `c4SchemaNegative.test.ts` and `lib/c4DisplayEvidence/__tests__/receipt.test.ts`.

## 1. Baselines

- [x] 1.1 The existing lane runs on node-27 against `https://test.nwm.ac.cn` (2026-09-26, `/home/nwm/tmp/q/`). With `…_shud_reach_000001` pins for `basins_shj`, `basins_haihe_ziyahe` and `basins_tailanhe`, all three **FAIL** at warmup with `HOOK_SELECTION_FAILED` "hook invocation failed".
- [x] 1.2 A diagnostic probe (read-only) finds the cause:
  - the hook rejects with `HOOK_FEATURE_MISMATCH`;
  - the rendered `m11-discharge-line` feature at the anchor carries `…_shud_shud_riv_000001`;
  - segment detail returns 200 for both id families.
- [x] 1.3 The hook-dispatch "before", with `…_shud_shud_riv_000001` pins (the old mechanism; t0 is taken right before a direct `onOverlayClick`). All three are PASS:

  | pin | P95 |
  |---|---|
  | `basins_shj` (29 428 segments) | **472.8 ms** |
  | `basins_haihe_ziyahe` (8 275) | **388.7 ms** |
  | `basins_tailanhe` (63) | **461.6 ms** |

  The durations are kept in `/home/nwm/tmp/q/baseline-*.log`. These numbers are not comparable to the new trusted-pointer t0 (D3).
- [x] 1.4 Product networks and segment counts come from `core.river_network_version`, read-only via the display role. `basins_shj_nj`, which #1342 names, has no current product (GFS and IFS latest are 404).

## 2. Implementation

- [ ] 2.1 **D1 hook** (`src/lib/riverClickEvidence/hook.ts`, wiring in `M11MapLibreSurface.tsx`):
  - `locateRenderedRiver`, with the DOM check (`elementFromPoint === canvas`) and the product priority-walk check through the resolver shared with `handleM11MapClick`, extracted from `m11MapInteractions.ts`. Either failing gives `HOOK_POINT_OCCLUDED`. `interactiveLayerIds` and the station flag are read through refs, and the ids are filtered by `map.getLayer`;
  - `armPointerCapture` / `takePointerCapture`: every canvas pointer-down is observed; exactly one trusted event is the result; none or unarmed gives `HOOK_POINTER_MISSING`; more than one or any untrusted gives `HOOK_POINTER_INVALID`;
  - `selectRenderedRiver` and the `onOverlayClick` parameter deleted;
  - the new codes added to the closed code set;
  - cleanup removes the listener.

  Unit tests:
  - the gate is absent without the flag;
  - locate resolves identity and point and calls no product callback; a spy on `onOverlayClick` is never called;
  - a DOM-covered point, and a point where the resolver picks a station, a cluster, basin fill or another river, each reject;
  - `handleM11MapClick` behaviour is unchanged after the resolver is extracted (its existing tests stay green);
  - an absent interactive layer id is filtered out and raises no map error;
  - a trusted capture returns the event's `timeStamp`;
  - untrusted (a synthetic `dispatchEvent`), duplicate, missing and unarmed captures each give their code;
  - stale-generation cleanup;
  - every existing readiness, limit and mismatch case, carried over.
- [ ] 2.2 **D2 lane** (`playwright.river-click-lane-attempt.ts` and the lane / terminal modules): the per-attempt sequence arm observation → arm capture → locate → `page.mouse.click` → take capture → t0 = capture `timeStamp`. Displacement over 2 px, or a capture error, gives `CLICK_DISPATCH_INVALID`. Hook rejections carry `hook <CODE>` in the message. Unit tests with the lane's fake page:
  - t0 comes from the capture;
  - a displaced capture FAILs;
  - an untrusted or missing capture FAILs;
  - `page.mouse.click` is called with the located point exactly once per attempt;
  - no code path calls a hook dispatch;
  - the propagated hook code appears in the message.
- [ ] 2.3 **D3 schema 1.1**: the JSON schema (`click_dispatch` const, `schema_version` "1.1", the new code), the three examples, the Node validator, and the binder (1.1 only; 1.0 gives a fixed `BINDER:` line). Tests:
  - the examples validate;
  - a 1.0 document is rejected by the validator and the binder;
  - a missing or wrong `click_dispatch` is rejected;
  - `CLICK_DISPATCH_INVALID` is accepted as a FAIL code.
- [ ] 2.4 **Live spec** (`e2e/live-display.spec.ts`): it uses the new lane and still passes the static no-mock guard. The Playwright profile, workers, retries and timeout are unchanged.
- [ ] 2.5 **Mocked and component suites**: any test that relied on `selectRenderedRiver` or on direct dispatch is rewritten to the new API. No mocked spec may emit a live receipt.
- [ ] 2.6 **Checks**: a repo-wide `grep` for `selectRenderedRiver` and `dispatchNowMs` returns nothing. Exempt: `openspec/changes/archive/**`, the main spec `openspec/specs/frontend-river-click-live-evidence/spec.md` (until the archive folds the delta in), this change's own files, and `docs/review-loop-log.jsonl`.
- [ ] 2.7 **D4 runbooks**:
  - `docs/runbooks/node-27-bringup-checklist.md` C4 ④⑤ and `docs/runbooks/tier-node27-timeseries-storage.md` §4.9: the real-click mechanism, schema 1.1, the pin rule (the discharge-layer `…_shud_shud_riv_…` id family, with the `HOOK_FEATURE_MISMATCH` symptom of a `shud_reach` id), the three-network selection rule with its read-only commands, and the three-receipt acceptance;
  - the "does not claim live PASS" sentence is updated once §5 lands.

## 3. Local verification

- [ ] 3.1 In `apps/frontend`:
  - `pnpm test`;
  - `pnpm typecheck`;
  - `pnpm check:types` (the Playwright/Node config);
  - `pnpm build`;
  - `pnpm check:api-types`;
  - `pnpm check:bundle`.
- [ ] 3.2 Root:
  - the schema-example loop, as CI runs it (JSON Schema Validate job);
  - `uv run ruff check .`;
  - `openspec validate river-click-trusted-pointer-dispatch --strict --no-interactive`.

## 4. node-27 pre-merge: throwaway stack, real clicks

- [ ] 4.1 Make a disposable worktree of the pushed SHA under `/home/nwm/tmp/` and run `corepack pnpm@10.11.0 install --frozen-lockfile` and `build` in it.
- [ ] 4.2 Start the throwaway API exactly as design D5 describes:
  - absolute `/home/nwm/NWM/infra/env/display.env`;
  - `NHMS_MVT_FILE_CACHE_DIR` overridden to a private dir under `/home/nwm/tmp`;
  - a redacted recorded check that the sourced `DATABASE_URL` user is `nhms_display_ro` and `NHMS_SERVICE_ROLE=display_readonly`. The API reads `DATABASE_URL` only;
  - `/home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port <free> --workers 1`, with cwd and `PYTHONPATH` set to the worktree;
  - not `start-display-api.sh`.

  Record the resolved `FRONTEND_DIST_DIR`, and check that the served `index.html` hash equals the worktree `dist`. Afterwards, stop the process by its recorded PID and delete the private cache.
- [ ] 4.3 **Merge gate.** Run the lane three times against `http://127.0.0.1:<port>` (both origins), one run per D4 pin. Record rc, status, P95, `click_dispatch` and the durations. Expected for every pin: rc 0, status PASS, `click_dispatch=trusted_pointer_event`, warmup 1 plus 20 samples, and no `CLICK_DISPATCH_INVALID`, `HOOK_*` or `SERIES_REQUEST_INVALID` failure. A mechanism failure blocks the merge. A `THRESHOLD_EXCEEDED` with complete samples is a product finding and is reported to the user before merge.

## 5. node-27 post-merge: public-site receipts

- [ ] 5.1 Deploy per design D5:
  - `git status --porcelain` → `git pull --ff-only` on `/home/nwm/NWM`;
  - `install --frozen-lockfile`;
  - build to `dist.new`; move any existing `dist.old` aside to a timestamped name; then the two-rename `mv -T` swap, keeping `dist.old` for rollback;
  - record the served `index.html` hash before and after.
- [ ] 5.2 Produce three receipts against `https://test.nwm.ac.cn` (D4 pins, re-checked live), each in a fresh private run directory, and run the binder on each one.
- [ ] 5.3 Write the receipt at `openspec/changes/archive/<date>-river-click-trusted-pointer-dispatch/evidence/node27-live-receipt.md` (in the archive PR). It holds the pins and their discovery evidence, the binder result, P95s and durations, `click_dispatch`, and the before/after comparison with §1.3. It is recorded honestly: a FAIL stays a FAIL.

## Evidence Floor

1. **No flag, no surface:** no global and no listener, and user behaviour is unchanged (2.1 tests, and the existing mocked suite is green).
2. **No injection path:** the hook has no method that calls `onOverlayClick`; a spy is never called and `grep` finds no `selectRenderedRiver` (2.1, 2.6).
3. **t0 is a trusted pointer event:** a trusted capture is accepted; untrusted, missing, duplicate and displaced captures FAIL `CLICK_DISPATCH_INVALID` (2.1, 2.2).
4. **Occluded points are refused:** `HOOK_POINT_OCCLUDED` (2.1).
5. **Schema 1.1:** the examples validate; 1.0 and a wrong `click_dispatch` are rejected by the validator and the binder (2.3).
6. **Live:** the node-27 throwaway-stack merge gate for three pins (§4.3 expected output, pre-merge), then three public-site binder-PASS receipts (§5, post-merge, archive PR). After merge, only a `THRESHOLD_EXCEEDED` with complete samples may be recorded as a non-PASS product finding. A mechanism failure (`CLICK_DISPATCH_INVALID`, `HOOK_*`, `SERIES_REQUEST_INVALID`) is a defect to fix, not a result to record.
7. **Docs:** the pin rule and the three-receipt command are in both runbooks (2.7).
