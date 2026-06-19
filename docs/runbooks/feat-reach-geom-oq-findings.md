# feat-reach-geom-from-river-shp — OQ Explorer Findings

Read-only exploratory evidence backing [openspec change `feat-reach-geom-from-river-shp`](../../openspec/changes/feat-reach-geom-from-river-shp/proposal.md) [Open Questions OQ1/OQ2/OQ3](../../openspec/changes/feat-reach-geom-from-river-shp/design.md#open-questions). Produced 2026-06-19 by 3 parallel read-only explorer subagents under [Issue #559 (Section 0)](https://github.com/DankerMu/SHUD-NWM/issues/559).

These findings drive concrete decisions on PR 1 / PR 2 / PR 6 scope before implementation begins.

---

## OQ1 — Does an existing CLI subcommand re-ingest a basin by name?

**Verdict:** NO

**Evidence:**

- [`workers/model_registry/cli.py:194-226`](../../workers/model_registry/cli.py#L194-L226) — `import-basins-registry` requires pre-built `--inventory` and `--package-manifest` JSON files; has no `--basin-slug` / `--basin-name` flag.
- [`workers/model_registry/cli.py:228-283`](../../workers/model_registry/cli.py#L228-L283) — `bootstrap-qhh-production` accepts `--basin-slug qhh` and `--model-id basins_qhh_shud`, but is hardwired around QHH conventions.

**Closest current entry:** [`bootstrap_qhh_production` (`workers/model_registry/qhh_production_bootstrap.py:198`)](../../workers/model_registry/qhh_production_bootstrap.py#L198) — accepts `qhh_basin_slug` and `model_id` but internally hardcodes QHH paths (`qhh.tsd.forc`, `qhh.sp.riv`), not generic for arbitrary basins.

**Implication for PR 3 (#562):** **must add new CLI subcommand** — a generic `reingest-basin --basin-slug <name>` that drives `discover → publish-package → import-registry` for any basin slug, generalizing QHH-specific path conventions.

---

## OQ2 — Does the frontend consume `river_segment_id` at SEGMENT- or REACH-level granularity?

**Verdict:** SEGMENT-LEVEL — fully wired end-to-end, no reach grouping anywhere.

**Hover/popup key:** [`apps/frontend/src/components/map/M11MapLibreSurface.tsx:327`](../../apps/frontend/src/components/map/M11MapLibreSurface.tsx#L327) —
```
const riverSegmentId = featureStringProperty(riverFeature, 'river_segment_id')
                    ?? featureStringProperty(riverFeature, 'segment_id')
```
State held in `hoveredRiverSegmentId` (line 238).

**Coloring key:** [`M11MapLibreSurface.tsx:1006`](../../apps/frontend/src/components/map/M11MapLibreSurface.tsx#L1006) — `'line-color': ['get', 'layer_color']`; `layer_color` computed per-segment in `buildBasinRiverFeatureCollection` ([`M11MapLibreSurface.tsx:1284`](../../apps/frontend/src/components/map/M11MapLibreSurface.tsx#L1284)) from each `BasinSegmentRow`'s `currentQ` / `returnPeriod` / `warningLevel`.

**Store grouping:** [`apps/frontend/src/api/overviewDataContracts.ts:679`](../../apps/frontend/src/api/overviewDataContracts.ts#L679) `normalizeBasinSegmentRows` → [`line 1437`](../../apps/frontend/src/api/overviewDataContracts.ts#L1437) `segmentRowFromFeature` — **no grouping; passes through API rows**. Each `ApiRiverFeature` from `GET /api/v1/basin-versions/{id}/river-segments` produces exactly one `BasinSegmentRow`, keyed by `props.river_segment_id` + `props.segment_id`.

**MapLibre promoteId:** [`M11MapLibreSurface.tsx:987`](../../apps/frontend/src/components/map/M11MapLibreSurface.tsx#L987) — `promoteId="river_segment_id"`, so MapLibre feature-state identity is per-segment.

**Tooltip path:** [`M11MapLibreSurface.tsx:532`](../../apps/frontend/src/components/map/M11MapLibreSurface.tsx#L532) — `features.find(f => f.properties.river_segment_id === hoveredRiverSegmentId || f.properties.segment_id === hoveredRiverSegmentId)`; displays segment-level `q_value`, `return_period`, `warning_level`.

**Forecast panel:** [`M11RiverForecastPanel.tsx:66-76`](../../apps/frontend/src/components/map/M11RiverForecastPanel.tsx#L66-L76) — click → popup uses `segment.river_segment_id` for forecast-series fetch.

**Implication for D2 + PR 1/2 design:** **P0 blocker** — naively switching `core.river_segment` row granularity from segment (3738 rows/qhh) to reach (1633 rows/qhh) and renaming `river_segment_id` to `<model>_reach_<iRiv:06d>` **silently breaks every frontend hover / popup / colouring / forecast-fetch path that currently keys on `river_segment_id`**. Three mitigation paths exist:

| Path | DB row granularity | API `river-segments` shape | Frontend | Cost |
|---|---|---|---|---|
| **A. Frontend follows DB** | reach-level (1633) | reach-level (1633), `river_segment_id=<model>_reach_<iRiv:06d>` | Hover/popup/colour/forecast collapse to reach granularity (1 popup per reach, 1 colour per reach) | minimal backend, moderate frontend rewrite (forecast-panel identity, popup content, colour mapping); product UX simplifies |
| **B. API preserves segment view** | reach-level (1633) | segment-level (3738), each feature carries reach geom (shared across segments of same reach) + crosswalk `iRiv`/`iEle` | unchanged | moderate backend (new API JOIN; ≈2.3× payload from geometry duplication); frontend untouched; per-segment colouring still works but draws overlapping lines (perceptually merges to reach colour anyway) |
| **C. API splits reach geom by length proportion** | reach-level (1633) | segment-level (3738), each feature carries a length-proportional **slice** of the reach polyline | unchanged | high backend (geometric slicing per segment using `sp.rivseg` Length field); restores per-segment visual distinctness; payload size unchanged from current; brings back some "synthetic geometry" risk |

---

## OQ3 — Are there active consumers of `/api/v1/tiles/river-network/...`?

**Verdict:** ACTIVELY CONSUMED

**Route definition:** [`apps/api/routes/flood_alerts.py:1143`](../../apps/api/routes/flood_alerts.py#L1143)

**Active consumers:**

- [`apps/frontend/src/api/types.ts:364`](../../apps/frontend/src/api/types.ts#L364) — generated OpenAPI types declare the route as a typed path
- [`services/tiles/mvt.py:784`](../../services/tiles/mvt.py#L784) — `build_layer_metadata()` emits `tile_url_template` into the `LayerMetadata` payload returned by `/api/v1/layers`
- [`services/tiles/mvt.py:1476`](../../services/tiles/mvt.py#L1476) — `resolve_tile_layer_identity()` emits `tile_uri_template`
- [`apps/frontend/src/components/map/M11MapLibreSurface.tsx:620`](../../apps/frontend/src/components/map/M11MapLibreSurface.tsx#L620) — `buildMvtTileUrlTemplate(metadata, replacements)` consumes the metadata to build MapLibre vector source `tiles: [...]`
- [`apps/frontend/src/lib/mvtLayerMetadata.ts:84-88`](../../apps/frontend/src/lib/mvtLayerMetadata.ts#L84-L88) — `metadataMatchesRun()` checks `source_refs.river_network_version_id` staleness
- [`services/production_closure/scale_validation.py:56`](../../services/production_closure/scale_validation.py#L56) — `MVT_ENDPOINT_REFERENCES` exercises the route as a live performance check
- [`tests/test_flood_alerts_api.py`](../../tests/test_flood_alerts_api.py) lines 1845, 1943, 2032, 2076, 2229, 2284, 2868, 3457, 3486, 3721, 5265 — FastAPI TestClient GET calls against the live route handler

**Implication for PR scope:** must extend PR 6 (#566) audit scope to:

- [`services/tiles/mvt.py:1473-1480`](../../services/tiles/mvt.py#L1473-L1480) — `tile_cache` source-version identity
- [`apps/api/routes/flood_alerts.py:1434-1487`](../../apps/api/routes/flood_alerts.py#L1434-L1487) — `_fetch_river_network_mvt_tile_bytes` SQL must match the new `core.river_segment.geom` MultiLineString shape (already MultiLineString since [migration 000037](../../db/migrations/000037_river_segment_multilinestring.sql), but the source row granularity changes)
- The MVT tile cache identity (`source_version`) auto-busts when `river_network_version_id` changes, so cache coherence holds; visual conformance still requires post-rollout MVT preview check on node-27

---

## Decisions captured

| OQ | Resolution | Captured in |
|---|---|---|
| OQ1 | PR 3 must add `reingest-basin` CLI subcommand (generic, not QHH-specific) | issue #562 task 3.1 already anticipates this |
| OQ2 | **P0 escalation needed**: choose path A / B / C and revise spec + PR 2 design accordingly | **awaiting user decision** |
| OQ3 | PR 6 audit scope extended to `mvt.py` + `flood_alerts.py` MVT SQL | needs to be added to issue #566 task list |
