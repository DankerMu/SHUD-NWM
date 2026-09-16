## Context

Change surface: AppShell/MonitoringPage/ModelAssetsPage, OverviewPage, M11BottomControlBar/M11FloatingControls, list_layers, tests/test_precip_overlay.py, docs/spec/04_api_design.md section 9.
Current baseline: fresh origin/master; existing unrelated dirty checkout is untouched.
The active national-timeline change carries the newer 84px header/64px bar contract; its layout delta must agree with this change.

## Goals / Non-Goals

Goals: finish #2023/#2128/#2142/#2039 with individually traceable evidence.
Non-goals: timeline request identity, bootstrap notice logic, cycle rollover (#2127/#2139/#2140); new lineage APIs/UI; changing authentication, live DB contents, precipitation raster/index behavior; production deployment/restart.
Must preserve: route RBAC/legacy redirects, header height, control bar source/cycle/time actions, visible attribution, existing legend non-overlap, ready-run catalog contents/metadata/pagination/cache behavior and explicit run_id rejection semantics.
Governing invariant: an unrelated hydrological run or page layout constraint must not hide an available capability, required attribution, or reachable page content.
Sibling surfaces: /ops and /monitoring share MonitoringPage; ModelAssetsPage shares AppShell; overview root min-height and all bottom floating layers share the same map box; default and explicit-run layer catalogs share precip definitions/metadata; API prose/OpenAPI/frontend consumers describe the same delivered route set.

## Decisions

Non-map page roots own h-full/min-height-constrained vertical overflow; do not make the universal main scroll. Remove the map root's 40rem minimum in favor of min-h-0 so a short viewport resizes rather than clips the map. No page content rearrangement.
Keep the bar height 64px and centered width; set bottom clearance to 40px, above the normal attribution band, and move dependent floating layers by the same 24px where applicable. This avoids shrinking source/cycle/time controls at 520/800 widths; do not hide, clip or replace attribution. Browser rectangles are authoritative.
Update both this layout delta and the unarchived national-timeline layout delta to avoid a future archive restoring 16px.
When no ready run exists, construct only run-independent precip using the existing public definition and layer_metadata; no run identity/digest queries on that branch. Reuse the existing catalog construction pattern without creating a generic registry or fake run.
Mark section 9 explicitly unimplemented/retired from current display scope and remove conflicting production response promises; do not touch model-asset lineage or historical archives.

## Risks / Trade-offs

Moving the bar reduces vertical map room → verify short viewport fit and all overlapping siblings using real browser boxes, not snapshots/classes.
Programmatic scrolling can hide overflow-hidden bugs → wheel scroll must change content geometry and reach the bottom without window scroll.
Catalog cache and pagination could mask a new branch → verify default/explicit-ready/no-run, pagination, and explicit missing/not-ready errors independently.
A mocked browser result is not a production deployment receipt → label node-27 mocked red/green and isolated live-API smoke separately, record exact source SHA/build and no deployment claim.

## Migration Plan

No migration. Verify in an isolated node-27 checkout/port with TMPDIR=/home/nwm/tmp; no production checkout pull/restart. Revert this PR to roll back source behavior.
Seams under test: browser wheel/DOM rectangles, HTTP catalog response and real error envelopes, current docs compared with runtime/OpenAPI and absent frontend lineage calls.
Required evidence: tasks.md E1–E8; every runtime regression must fail against baseline sources before passing the fix.
Review focus: complete route coverage; attribution/sibling rectangles; no-run branch identity independence; unchanged explicit-run contract; no false lineage implementation claims.
Open questions: none; runtime evidence may reveal a concrete implementation adjustment without weakening these contracts.
