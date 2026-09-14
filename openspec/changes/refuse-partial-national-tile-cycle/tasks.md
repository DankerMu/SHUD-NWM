## Risk Triage

```text
Issue type: product/api contract change (#2153, owner ruling a′ 2026-09-14)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium
Fixture level: expanded
Repair intensity: medium
Upstream suggested level: absent (issue-scribe issue, size S for b/c, M for a; a′ sits between)
Why:
- public unauthenticated tile route gains a new refusal verdict and error code; OpenAPI response shape for one path changes
- shared coverage predicate is extracted from the per-cycle valid-times branch (must stay byte-identical there)
- runs inside the tile-generation lock on the cache-miss path (cache/lock interplay)
- kept at expanded, not high: no schema/migration, no auth change, no frontend runtime change, status code already declared on the route
Selected risk packs:
- Public API / CLI / script entry
- Concurrency / shared state / ordering (two READ COMMITTED statements; lock + cache miss path)
- Published NHMS artifacts / display identity (domain)
- Hydro-met time series / forcing windows (domain: (source, cycle) identity coverage)
OpenSpec change: refuse-partial-national-tile-cycle (generated)
Evidence floor:
- openspec validate refuse-partial-national-tile-cycle --strict --no-interactive
- openspec validate display-v2-national-timeline-precip-overlay --strict --no-interactive
- uv run pytest tests/test_hydro_display_mvt_scaling.py tests/test_openapi_drift.py tests/test_api_contract.py -q
- uv run ruff check services/tiles/mvt.py apps/api/routes/hydro_display.py apps/api/openapi_patching.py tests/test_hydro_display_mvt_scaling.py
- cd apps/frontend && pnpm check:api-types && pnpm exec tsc --noEmit -p tsconfig.app.json
- node-27 (isolated worktree of the PR head, throwaway DB, not blocked by design D6): export TMPDIR=/home/nwm/tmp && uv run pytest tests/test_mvt_national_identity_probe_integration.py -q
```

## Risk Pack Selection

| Pack | Selected | Reason |
|---|---|---|
| Public API / CLI / script entry | selected | New 424 code on a public route; OpenAPI response for that path. |
| Config / project setup | not selected | No env/config. `NHMS_ENABLE_LIVE_POSTGIS_MVT` gate order is preserved (E6). |
| File IO / path safety / overwrite | not selected | File cache format untouched; a refusal writes no entry (E1). |
| Schema / columns / units / field names | not selected | No DB schema; the error `details` keys are new and additive only. |
| Auth / permissions / secrets | not selected | Route stays public; details carry counts, never network ids. |
| Concurrency / shared state / ordering | selected | Active set and coverage rows come from two statements (set compare, E4); refusal inside `tile_generation_lock` (E1 lock unlinked). |
| Resource limits / large input / discovery | not selected | One pair of per-cycle statements per miss; cost residual recorded (design D5), live measurement deferred (D6). |
| Legacy compatibility / examples | not selected | Legacy source-less route explicitly untouched (E7). |
| Error handling / rollback / partial outputs | not selected | The refusal reuses the existing producer-raises path; covered by E1. |
| Release / packaging / dependency compatibility | not selected | None. |
| Documentation / migration notes | not selected | No runbook claims the old behaviour; the #2153 receipt follow-up is tracked (D6). |
| Published NHMS artifacts / display identity (domain) | selected | Fully covered tiles keep bytes/cache key/headers (E2); cached partial tiles residual (D5). |
| Hydro-met time series / forcing windows (domain) | selected | Coverage rule shared with per-cycle valid-times (E5). |

## Must preserve

- Fully covered identity: status 200, response bytes, `cache_key`, `source_version` and every `X-Tile-*` header identical to pre-change.
- No-run identity: HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`, same message and details, as pinned by the display-v2 `mvt-tile-contract` scenarios ("The identity probe binds…", "One source has a run and the other does not") and existing tests.
- Live PostGIS disabled / sqlite: the existing 424 byte-identical (including `details.layer_id = "hydro-national"`) with zero coverage statements.
- `national_discharge_valid_times` (both branches) and `national_discharge_cycles`: results, statements and binds unchanged; every existing test in `tests/test_hydro_display_mvt_scaling.py` unmodified. The single expected deviation is `test_each_national_route_hands_the_digest_helper_its_own_identity` (`len(session.digest_params) == 1` would count the new statements): it is resolved by changing `_NationalRouteSession` to route the active-network and coverage statements to their own recorders — never by editing the assertion. The fake's default answers must make active = coverage = `{rnv_a}`, so every existing 200 case keeps its meaning. Any other test edit is a reported deviation.
- Generic `_fetch_postgis_tile_bytes` unchanged (other layers, direct integration callers).
- Legacy `/api/v1/tiles/hydro-national/{variable}/{valid_time}/...` route: statements, verdicts and cache identity unchanged.
- `NATIONAL_DISCHARGE_QUERY_VERSION` literal unchanged.
- OpenAPI for the other five MVT paths byte-identical; `MvtLivePostgisUnavailable` component unchanged.

## Seams under test

- `services/tiles/mvt.py` helper and `national_discharge_valid_times` with `_NationalDiscoverySession` (active set + coverage rows fakes).
- Route level via `_request_national_identity_tile` / `_NationalRouteSession` and `TestClient`, with `read_cached_tile_response` / `build_raw_tile_response` monkeypatched (existing pattern). The fake records SQL TEXT per statement (not only binds) and classifies statements by distinguishing features:
  - digest: `geometry_generation`;
  - active set: `core.model_instance mi` without `hydro.hydro_run`;
  - coverage rows: `PARTITION BY mi.river_network_version_id, h.cycle_time` / `segment_count, river_sample_count`;
  - tile: `ST_AsMVT`.
  - The `_NationalDiscoverySession` classification is not reusable at route level: digest and tile SQL also mention `run_display_coverage`.
- Real DB: `tests/test_mvt_national_identity_probe_integration.py` `national_tile` fixture (throwaway DB migrated from zero) with `_seed_second_network`, `_seed_mixed_national_networks` and `_request_identity_tile`, run on node-27.
- OpenAPI drift/contract tests and `pnpm check:api-types`.

## Tasks

### 1. Fixture

- [x] 1.1 Proposal, design, tasks, ADDED delta for `mvt-tile-contract`.
- [x] 1.2 Fixture review; `openspec validate` strict for this change and display-v2.
  - Iteration 1 (revise, 4 gaps), all applied:
    - D2: explicit gate call in the national fetch, generic fetch untouched;
    - route fake session: statement classifier plus a single named expected deviation;
    - E2 rewritten to what the fake can prove, plus the real-DB E11;
    - D4 tooling facts.
  - Notes applied: D3 race edge; D5 third snapshot; E1 lock note; E5 patch-site note; D6 receipt control; proposal line reference.
  - Iteration 2 pass; notes applied: E11 coverage refresh, E11(c) comparison fields, proposal Impact lists the integration test file.

### 2. Coverage helper

- [x] 2.1 Extract the per-cycle set-equality coverage into one helper in `services/tiles/mvt.py`; `national_discharge_valid_times`' per-cycle branch reads through it.
- [x] 2.2 Tests E5.

### 3. Route refusal

- [x] 3.1 In the canonical national producer path: after `_require_live_postgis_mvt`, before the tile SQL, guarded by non-NULL `source`/`cycle`, evaluate the helper and raise 424 `MVT_NATIONAL_IDENTITY_INCOMPLETE` for ∅ ≠ covered ≠ active (design D3 body).
- [x] 3.2 OpenAPI (design D4), in this order:
  - the `_patch_mvt_tile_openapi` loop points the canonical path's 424 at the new `MvtNationalIdentityUnavailable` component, created by the patcher;
  - hand-edit `openapi/nhms.v1.yaml` (that path's 424 plus `components/responses`, and the docstring description if the docstring changes);
  - `cd apps/frontend && pnpm generate:api`.
- [x] 3.3 Tests E1–E4, E6–E8.
- [ ] 3.4 Integration test E11 (node-27 throwaway DB).

### 4. Close-out

- [ ] 4.1 File the node-27 post-deploy receipt follow-up issue (design D6), link it from the PR body.
- [ ] 4.2 PR closes #2153.
- [ ] 4.3 After merge: archive this change.

## Evidence Mapping

| ID | Seam | Input | Expected |
|---|---|---|---|
| E1 | route, live PostGIS enabled, cache miss (`read_cached_tile_response` → None), fake session: active `{rn-a, rn-b, rn-c}`, coverage rows for `rn-a`, `rn-b` at `(gfs, C)` | GET canonical tile | 424, `error.code == MVT_NATIONAL_IDENTITY_INCOMPLETE`, `details` counts 2/3 plus `layer_id`/`source`/`cycle`; no statement containing `ST_AsMVT` executed; `build_raw_tile_response` not called. Red pre-change (200 with tile bytes). (Lock-file absence may be asserted too, but the success path also unlinks, so it is not the discriminating proof.) |
| E2 | same, coverage rows for all three networks | GET canonical tile | 200; the active-set and coverage statements each executed exactly once, coverage binds `{source: "gfs", cycle: C, since: None}`; statement order digest → active → coverage → `ST_AsMVT`; `X-Tile-Cache-Key` and the tile binds equal **literals captured on the pre-change tree** (not recomputed from the post-change `TileInput`). The byte-level "unchanged" claim is proven by E11, not here |
| E3 | same, active `{rn-a, rn-b}`, zero coverage rows; tile SQL row `source_identity_count = 0` | GET canonical `ifs` tile | 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`, message/details identical to pre-change; green before and after |
| E4 | same, active `{rn-b, rn-c1, rn-c2}`, coverage `{rn-a, rn-c1, rn-c2}` | GET canonical tile | 424 `MVT_NATIONAL_IDENTITY_INCOMPLETE` with counts 3/3 (non-vacuity: equal sizes asserted). Red pre-change |
| E5 | `_NationalDiscoverySession` | helper on partial / full / empty / equal-size-mismatch inputs; `national_discharge_valid_times(source=, cycle=)` on the same inputs | helper completeness verdicts match D3; every existing per-cycle valid-times test green and unmodified; one test proves both call sites use the helper (monkeypatch the helper to report incomplete and assert valid-times returns `[]` AND the route refuses); if `hydro_display` imports the helper by name, patch it in both modules or call it through the module attribute so the patch reaches the route |
| E6 | route with cache hit (`read_cached_tile_response` returns a `TileResponse`), and separately with `NHMS_ENABLE_LIVE_POSTGIS_MVT` unset | GET canonical tile on a partial-coverage session | hit: 200 cached bytes, zero coverage statements executed; disabled: existing 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`, zero coverage statements |
| E7 | legacy route `/api/v1/tiles/hydro-national/{variable}/{valid_time}/...` on a partial-coverage session | GET legacy tile | no coverage statement executed; verdict/bytes as pre-change |
| E8 | OpenAPI | `uv run pytest tests/test_openapi_drift.py tests/test_api_contract.py -q`; `pnpm check:api-types` | green; canonical path 424 refs `MvtNationalIdentityUnavailable` with both codes; the other five MVT paths still ref `MvtLivePostgisUnavailable` (assert in a test) |
| E9 | red proof | pre-change tree | E1, E4 and E11(a) red (200); E3, E6, E7, E11(b)(c) green; E2's captured literals come from this run; recorded under `.workplans/issue-2153/evidence/` |
| E10 | toolchain | evidence floor commands | all green; `tests/test_hydro_display_mvt_scaling.py` run in full locally (CI selects files) |
| E11 | real DB, node-27 throwaway DB (`national_tile` fixture), isolated worktree of the PR head | (a) `_seed_second_network()` with no run for the cycle, then `_request_identity_tile(client, "gfs", _CYCLE_TIME, <a valid instant>)`; (b) `_seed_second_network(active=False)`, same request; (c) `_seed_mixed_national_networks` (both networks covered), same request | (a)(b) call `_refresh_coverage(database_url)` after seeding and request `_WINDOW_END` (the `_seed` fixture deliberately leaves `run_display_coverage` unrefreshed; without the refresh covered = ∅ and (a) proves nothing); (a) 424 `MVT_NATIONAL_IDENTITY_INCOMPLETE` with counts 1/2 (red pre-change: 200); (b) 200; (c) 200, and the test writes `sha256(body)` and `ETag` into its evidence output so the pre-change and PR-head runs are compared on those two fields; the whole file stays green. Output recorded under `.workplans/issue-2153/evidence/` |
