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
- The list was the only READER of `core.river_segment_crosswalk` in `apps/` and `packages/`; after this
  change the table is still written by import but no API path reads it. Stated, not hidden: the living
  spec lines that speak of crosswalk "hover/click lookups" (`basins-registry-import/spec.md:158-162`,
  `:255`) lose their only API-level verifier. Whether the crosswalk itself is still wanted is a separate
  question, not decided here.
- `docs/governance/API_CONTRACT_DEPRECATION_POLICY.md` says OpenAPI must not contract while external
  consumers are unknown; the retirement record cites the owner's 2026-10-12 ruling (no external users) as
  the basis, so the two governance documents do not contradict each other.
- Docs: `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` gets the retirement record;
  `docs/runbooks/display-mobile-evidence.md` drops the "do not take a pin from the list" workaround in
  favour of the fact that there is no list.

## Scope correction found during implementation (orchestrator ruling, 2026-10-12)

The store's list entry (`PsycopgModelRegistryStore.list_river_segments`, its four private helpers and the
`RIVER_SEGMENT_COLLECTION_*` constants) is **kept** in this change. Five integration tests in
`tests/test_basins_registry_import_db.py` and `tests/test_basins_registry_import_qhh.py` call it, and
those files are the #1913 frozen partition: every `test_*` definition is pinned by source and AST sha256
in `tests/fixtures/basins_registry_partition_oracle.json` and guarded by `tests/test_select_ci_tests.py`.
Re-pointing or deleting them is an oracle transition — a governance change with no precedent for
removals — and does not belong in an API retirement PR.

What this change therefore delivers: the HTTP surface is gone (route, OpenAPI, generated types, response
models, frontend caller), so no API response can carry a `_seg_` id any more. What it leaves: a store
method with no API caller, reachable only from those frozen tests and its own unit tests. Removing it,
with the oracle transition, is a follow-up issue.

## Out of scope

- The `hydroMetProductData.ts` / `useHydroMetProduct.ts` chain (no product consumer) stays; it only
  loses its dependency on the list. Removing the chain is a separate decision.
- `core.river_segment` row design (#1693), the `_reach_` -> `_shud_riv_` series translation (#577), MVT,
  the river click hook, import / crosswalk writing, any migration or production data.
- Historical documents (`docs/review/**`, rollout receipts, archived changes) are not rewritten.
- Production deploy on node-27 (the no-pull constraint from #1993 holds until the owner lifts it).
