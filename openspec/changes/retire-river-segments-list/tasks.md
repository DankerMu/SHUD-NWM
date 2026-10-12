# Tasks

## 1. Retire the list endpoint (#2866)

- [ ] 1.1 Backend: delete the list route in `apps/api/routes/models.py`, the store list entry + Path C
      slice branch in `packages/common/model_registry_river_segments.py` (and re-exports in
      `model_registry.py` / `model_registry_public.py` / `model_registry_contracts.py`) **only where no
      caller other than the route remains**; delete the response models / OpenAPI patches that only the
      list used. Any non-route caller found (scripts, services, tests of import correctness) is reported
      and kept working — do not delete a helper that something else still calls.
- [ ] 1.2 Frontend: delete `fetchHydroMetRiverSegments`, its call, the `riverSegments` field and now-unused
      types / tests; regenerate `apps/frontend/src/api/types.ts`; `pnpm check:api-types` passes.
- [ ] 1.3 Tests: a contract test that the list path is absent from the OpenAPI document and is not
      served, and that detail still answers for a reach id and an output-river id (existing tests may
      already pin the latter — name them). Tests that pinned the list shape are deleted or re-pointed,
      each listed in the PR with its disposition. Tests that used the list only as a convenient way to
      observe import results are re-pointed at the store / SQL, not deleted.
- [ ] 1.4 Docs: retirement record in `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md`;
      `docs/runbooks/display-mobile-evidence.md` workaround replaced; `docs/spec/04_api_design.md` and
      other LIVING docs that advertise the list updated; `services/production_closure/*` fixture
      dictionaries that name the list URL updated or reported.

## 约定

- **Fixture level:** compact. **Risk packs:** api-contract, frontend type sync.
- **Must preserve:** detail endpoint and forecast-series behaviour for `_reach_` and `_shud_riv_` ids;
  import and `core.river_segment_crosswalk` writing; MVT; every other `/basin-versions/{bv}/...` route;
  no migration, no schema change.
- **Non-goals:** see proposal "Out of scope".
- **Evidence floor:** `uv run ruff check .`; touched pytest files green; the new contract test red
  against the pre-change tree; `pnpm tsc` / `pnpm test` / `pnpm check:api-types` green;
  `openspec validate retire-river-segments-list --strict`; `git grep "_seg_"` in `apps/ packages/`
  shows no remaining producer of the derived id. node-27 real-DB pytest for the touched integration
  files from the verify checkout. No production deploy.
