# Display follow-up batch: node-27 acceptance receipt

Issues: #2023, #2128, #2142, #2039. Date: 2026-09-16.

## Identity and scope

- Baseline source: `7ecc46bed18cc8b6f20030f49aa3bf43a77b6858`.
- Fixed source/tests: `e0775fde9389aea1c10fea8259956bf5e7df9626`; sanitized capture helper:
  `9566c155805e74943d7fba6e6329f252a5b3a0a7`. The latter changes only screenshot redaction, not
  application code.
- Isolated checkout: `/home/nwm/worktrees/issue-2023-display-batch`. No production checkout pull,
  service restart, production frontend replacement, or DB mutation was performed.
- Browser: node-27 Playwright 1.59.1 bundled headless Chromium. Mocked lane used its own Vite server
  on loopback port 18203.
- Live lane: isolated target API + target frontend on `127.0.0.1:18023`, existing readonly display
  environment, separate `/home/nwm/tmp/issue2023-mvt-cache`, real readonly database/API responses.
  Runtime reported `display_readonly`, `control_mutations_enabled=false`,
  `slurm_routes_enabled=false`.
- Live builds used `VITE_API_BASE_URL=` and separately baked `VITE_AUTH_ROLE=viewer`, `operator`,
  `model_admin`. This is test-only build-time role injection, not production authentication proof.
  The dev Role selector was absent in built operational pages.

## Red → green

Only the six affected runtime source files were restored from baseline in the isolated checkout;
regression tests stayed at the fixed revision. The sources were restored to
`e0775fde9389aea1c10fea8259956bf5e7df9626` before green runs.

| Gate                                               | Baseline                                                                                                                            | Fixed                                                                        |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| New no-run catalog tests in precip + scaling files | 2 failed: actual `[]` vs required precip-only                                                                                       | Included in 30 passing catalog/layers tests                                  |
| Three operational wheel regressions                | All 3 failed: page root expanded beyond main instead of becoming a scroll container                                                 | All 3 passed; wheel moves content to bottom without window scroll            |
| Short 1280x600 map fit                             | Failed: map bottom 724 while main bottom 600                                                                                        | Passed: map/main bottom 600, map height 516                                  |
| 1280x800 map regression                            | Passed baseline, retained compatibility                                                                                             | Passed                                                                       |
| Five control-bar geometry cases                    | Failed 40px bottom-clearance assertion on baseline 16px layout; measured boxes additionally prove real intersection at 1280/800/520 | All pass: 64px height, 40px bottom clearance and nonintersecting attribution |
| Full two-file mocked lane                          | Targeted baseline: 9 failed, 1 passed                                                                                               | 31 passed                                                                    |

Baseline collision evidence is not inferred from CSS: at 1280x900 bar is `(128,820,1024,64)` and
attribution `(1126,866,144,24)`, overlapping **26×18px**. Fixed bar is `(128,796,1024,64)`, leaving
**6px vertical clearance**. The issue's historical 27px horizontal estimate used a 145px
attribution; this node-27 run measured 144px.

Commands and complete output: [red catalog](node27-red-catalog.log),
[red browser](node27-red-browser.log), [green catalog](node27-green-catalog.log),
[green browser](node27-green-browser.log).

## Real readonly API browser smoke

The browser loaded target built application bytes and real API responses; no page.route/HAR/mocked
payloads. Each role run passed.

| Route                | Viewport | Measured scroll top, before → bottom          | Window x/y |
| -------------------- | -------- | --------------------------------------------- | ---------- |
| /ops                 | 1280x600 | 0 → 377; scrollHeight 893, clientHeight 516   | 0/0        |
| /monitoring          | 1280x600 | 0 → 1068; scrollHeight 1584, clientHeight 516 | 0/0        |
| /system/model-assets | 1280x600 | 0 → 5090; scrollHeight 5606, clientHeight 516 | 0/0        |

Map/attribution rectangles were measured at 1920x1080, 1440x900, 1280x900, 800x900, 520x900 and
1280x600. All bar/attribution and legend/attribution pairs were separate; the short map fit its
viewport. Screenshots in `live/` were inspected, including 1280/520 map and /ops bottom.

Live logs: [viewer sanitized](node27-live-viewer-sanitized.log),
[operator](node27-live-operator.log), [model admin](node27-live-model_admin.log). Build logs record
asset filenames. `live/requests-*.json` records API method/path only; every observed API request was
GET/HEAD.

**Limitations:** an external Tianditu vector-base request returned HTTP 403 and the existing
map-source-error banner echoed its client-key URL. This unrelated provider failure is NOT fixed or
claimed healthy by this batch. Map screenshots redact only that banner (magenta rectangle); measured
map controls remain unmasked. Hydrology/precip layers and layout were observable. /ops'
automatically chosen current cycle had no forecast/jobs and displayed the existing honest empty
state; this run proves scrolling/layout, not fresh forecast availability. At 520px, the existing
dense timeline labels remain crowded; this batch does not redesign timeline typography.

## Other verification

- Local `pnpm test`: **81 files / 1052 tests passed**.
- Local `pnpm exec tsc --noEmit -p tsconfig.app.json`, `pnpm build`, `pnpm check:api-types`,
  `uv run ruff check .`, strict OpenSpec validation: passed.
- E4 node-27
  `uv run --no-sync pytest -q tests/test_precip_overlay.py tests/test_hydro_display_mvt_scaling.py -k 'catalog or layers'`:
  **30 passed**, including explicit missing/not-ready run errors and pagination compatibility.
- #2039 runtime/OpenAPI route inventory: **zero display lineage routes**
  ([output](lineage-runtime-baseline.log)). Current frontend has no
  fetchLineage/loadBasinDetail/river-point caller; current docs now say unimplemented. Original UI
  removal was already delivered by #2331; this batch does not recreate it.
- This receipt is not a production deployment, a full C1–C3 audit, provider health acceptance, or
  the previously closed #2127/#2139/#2140 acceptance suite.

## Cleanup

The isolated API was stopped after capture. SSH supervision initially left its remote foreground
child alive; PID was verified by command line and cwd as this isolated uvicorn before sending TERM.
Production `:8080` was never stopped. Raw API responses/credentials were not copied into the
receipt.
