## 1. Fixture

- [x] 1.1 Complete expanded fixture, read-only fixture review, strict validation.

## 2. Implementation

- [ ] 2.1 #2023: internal wheel scrolling on /ops, /monitoring and /system/model-assets; short map fit; regression coverage.
- [ ] 2.2 #2128: separate control bar/attribution rectangles, align adjacent overlays and active layout spec; regression coverage.
- [ ] 2.3 #2142: no-ready-run precip catalog branch with unchanged existing contracts; route regression coverage.
- [ ] 2.4 #2039: reconcile current API design section 9 with retired frontend lineage; preserve other provenance.

## 3. Evidence and review

- [ ] 3.1 E1/E2/E3: node-27 red/green scrolling and collision regression with measured boxes, and map fit.
- [ ] 3.2 E4: node-27 catalog HTTP regression red/green plus ready/missing/not-ready/pagination compatibility.
- [ ] 3.3 E5/E6: local frontend unit/type/build/API-type checks, ruff and OpenSpec validation; current lineage consumer/schema inventory.
- [ ] 3.4 E7: node-27 isolated target build + real display API browser smoke, no production mutation; capture screenshot/geometry and identify test-only role override.
- [ ] 3.5 E8: cross-review, adjudication, clean SHA gate and green CI; close each issue only when its evidence row is satisfied.

## Evidence Floor

| ID | Issue / scenario | Oracle and command | Required observation |
| --- | --- | --- | --- |
| E1 | #2023, three operational routes | node-27: corepack pnpm exec playwright test e2e/monitoring.mocked.spec.ts --project=mocked-regression-chromium --workers=1 | Use operator for /ops and /monitoring; model_admin or sys_admin for /system/model-assets. At 1280x600 real wheel changes content y/scrollTop, reaches bottom, computed overflow auto/scroll, window unchanged; new regressions fail baseline and pass fixed source. |
| E2 | #2023 map | node-27 browser same lane | / at 1280x800 has no window/main scrolling, map fills available area; short 1280x600 fits map rather than clipping its bottom. |
| E3 | #2128 | node-27: corepack pnpm exec playwright test e2e/m11-overlay-collision.mocked.spec.ts --project=mocked-regression-chromium --workers=1 | Bar/attribution nonintersection and attribution visibility at 1920x1080,1440x900,1280x900,800x900,520x900; retain legend/attribution guard and verify adjacent overlays do not cover bar; record boxes in both red and green runs. |
| E4 | #2142 | node-27: uv run pytest -q tests/test_precip_overlay.py tests/test_hydro_display_mvt_scaling.py -k 'catalog or layers' | No-ready-run HTTP 200 contains exactly precip with byte-equivalent entry to explicit-ready-run; pagination still works; missing explicit run 404 and not-ready 409 unchanged; ready catalogs unchanged. Migrate scaling sibling test_layer_catalog_is_empty_when_no_run_is_display_ready to the new precip-only contract; run its unknown-run and cache-pagination compatibility cases too. New no-run regression fails baseline then passes fix. |
| E5 | all | local apps/frontend: pnpm test; pnpm exec tsc --noEmit -p tsconfig.app.json; pnpm build; pnpm check:api-types; local root: uv run ruff check .; openspec validate display-followup-layout-catalog-contracts --strict --no-interactive | All pass; no generated schema changes required. |
| E6 | #2039 | source search plus node-27 runtime route/OpenAPI inventory | No display lineage request/interface/mock restored; docs mark all three proposed display lineage endpoints unimplemented, not delivered; model-asset provenance unchanged. Prior PR #2331 removed original UI failure; no new lineage API/UI is required. |
| E7 | #2023/#2128 live surface | node-27 isolated frontend build on unused loopback port, real readonly API; browser smoke | / loads as viewer, /ops and /monitoring as operator, /system/model-assets as model_admin or sys_admin. Observe target SHA, scrolling and visible/nonoverlapping attribution; screenshot and boxes; no production service/config/data changes. Test-only RBAC override is disclosed. This is not a C1-C3 redeployment claim. |
| E8 | all | independent reviews + fix_gate + GitHub CI | Reports, verdicts, exact reviewed head, complete issue-to-evidence mapping, passing merge checks. |

## Risk packs

| Pack | Selection / reason | Evidence |
| --- | --- | --- |
| Public API / CLI / script entry | Selected: public layers changes zero-run behavior | E4/E6 |
| Config / project setup | Not selected: no permanent config changes | Non-goal |
| File IO / path safety / overwrite | Not selected: no new runtime IO | Non-goal |
| Schema / columns / units / field names | Not selected: existing Layer fields/metadata reused unchanged | E4/E5 |
| Auth / permissions / secrets | Not selected: preserve RBAC, no permission change | E1/E7 test-only override disclosed |
| Concurrency / shared state / ordering | Not selected: no cache/nonce/async changes | Existing cache path preserved; E4 |
| Resource limits / large input / discovery | Not selected: fixed four-entry catalog, layout only | Non-goal |
| Legacy compatibility / examples | Selected: shell routes and existing run catalogs | E1–E6 |
| Error handling / rollback / partial outputs | Selected: absent run vs explicitly invalid run | E4 |
| Release / packaging / dependency compatibility | Not selected: no dependency or release config change | E5 |
| Documentation / migration notes | Selected: lineage and layout promises | E3/E6 |

## NHMS domain risk packs

| Pack | Selection / reason | Evidence |
| --- | --- | --- |
| Geospatial / CRS / basin geometry | Not selected: no geometry or CRS changes | Non-goal |
| Hydro-met time series / forcing windows | Not selected: raster/index/window semantics unchanged | Non-goal |
| SHUD numerical runtime / conservation / NaN | Not selected: no SHUD changes | Non-goal |
| PostGIS / TimescaleDB domain behavior | Not selected: no SQL/schema behavior changes | Non-goal |
| Slurm production lifecycle / mock-vs-real parity | Not selected: no scheduler changes | Non-goal |
| External hydro-met providers / snapshot reproducibility | Not selected: no provider changes | Non-goal |
| Run manifest / QC provenance | Not selected: model-asset provenance retained | E6 |
| Published NHMS artifacts / display identity | Selected: zero-run catalog changes to precip-only | E4 includes scaling-file no-run, explicit-run and pagination cases |

## Evidence receipts

Pending execution; no pass is claimed by fixture creation.
