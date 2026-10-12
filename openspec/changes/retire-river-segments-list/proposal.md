# Retire the river-segments list endpoint and its derived `_seg_` id

Issue: #2866 (owner ruling 2026-10-12: option 1 — retire; the system has no external API users).
Fixture level: **compact**. Risk packs: api-contract (a published path is removed), frontend type sync.
Review seat: one (`correctness` + `test-evidence+spec-compliance`).

## Why

`GET /api/v1/basin-versions/{bv}/river-segments` returns features whose id is a derived
`<model_id>_seg_<iRiv>_<iEle>` (built at `packages/common/model_registry_river_segments.py:628`). That id
is no row of `core.river_segment`: the detail endpoint and forecast-series both answer 404 for it, and
the MVT layer renders a third family. The spec's only reason for `_seg_` was a frontend GeoJSON layer
that no longer exists (the river layer is MVT). The list has no product caller: its one frontend caller
(`fetchHydroMetRiverSegments`) feeds a value that is discarded.

## What Changes

- **BREAKING (API):** remove the list route `GET /api/v1/basin-versions/{bv}/river-segments` and the
  store code that only it reaches (the list entry and the Path C slice branch), its response models and
  OpenAPI patches. Detail and forecast-series are untouched.
- Frontend: remove `fetchHydroMetRiverSegments`, its call in `loadHydroMetBootstrap`, and the
  `riverSegments` field / types that carry its result; regenerate `src/api/types.ts`.
- Spec: the list scenario is replaced by a retirement scenario.
- Docs: `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` gets the retirement record;
  `docs/runbooks/display-mobile-evidence.md` drops the "do not take a pin from the list" workaround in
  favour of the fact that there is no list.

## Out of scope

- The `hydroMetProductData.ts` / `useHydroMetProduct.ts` chain (no product consumer) stays; it only
  loses its dependency on the list. Removing the chain is a separate decision.
- `core.river_segment` row design (#1693), the `_reach_` -> `_shud_riv_` series translation (#577), MVT,
  the river click hook, import / crosswalk writing, any migration or production data.
- Historical documents (`docs/review/**`, rollout receipts, archived changes) are not rewritten.
- Production deploy on node-27 (the no-pull constraint from #1993 holds until the owner lifts it).
