## Context

- The canonical route `hydro_national_source_cycle_mvt_tile` (`apps/api/routes/hydro_display.py`) runs in this order:
  1. validates shape;
  2. computes `national_discharge_source_version(session, source, cycle, valid_time)` (a digest statement on every request);
  3. calls `_cached_or_generated_mvt_response(session, tile_input, producer)`, which:
     - serves the file cache on a hit;
     - on a miss, takes `tile_generation_lock`, re-checks the cache, and runs `producer()`, i.e. `_fetch_hydro_national_mvt_tile_bytes` → `_fetch_postgis_tile_bytes`. That function first applies `_require_live_postgis_mvt` (424 `MVT_LIVE_POSTGIS_UNAVAILABLE` when disabled or sqlite), then runs the tile SQL, and raises 424 `MVT_LIVE_POSTGIS_UNAVAILABLE` when `source_identity_count <= 0`.
- The tile SQL's `latest_runs` CTE is a per-network `DISTINCT ON` for `(source, cycle)`, clamped to the instant's coverage window, with no "every active network" requirement. The identity probe only needs one matching network.
- `national_discharge_valid_times(session, source=, cycle=)` reads `_national_discharge_coverage_rows(session, source=, cycle=, since=None)`, then returns `[]` unless `frozenset(covered networks) == active_networks` (set comparison, `services/tiles/mvt.py` per-cycle branch).

## Decisions

### D1: predicate = the per-cycle valid-times set equality, one owner

- Add one helper in `services/tiles/mvt.py`, e.g. `national_discharge_cycle_coverage(session, *, source, cycle)`.
  - It calls `_national_discharge_coverage_rows(session, source=source, cycle=cycle, since=None)`.
  - It returns the coverage rows, the covered-network set and the active-network set (a small frozen dataclass or NamedTuple).
- `national_discharge_valid_times`' per-cycle branch reads through it; its behaviour, statements and binds stay byte-identical. The tile route uses the same helper.
- "Complete" means `covered == active` as SETS, never cardinalities: the matrix-40b fail-open case already pinned by `test_national_per_cycle_valid_times_fail_closed_when_a_network_activates_between_the_two_statements`.
- Rejected:
  - `/cycles` membership (option a): it would also reject aged but fully covered cycles that the frontend deep-links and renders today.
  - A `valid_time`-clamped per-network check: stronger, but a second rule the valid-times endpoint does not apply; see D5.

### D2: placement = miss path only, after the live-PostGIS gate, before the tile SQL

- The check lives in `_fetch_hydro_national_mvt_tile_bytes` only, guarded by `source is not None and cycle is not None`, so the legacy route (NULL identity) never runs it. The generic `_fetch_postgis_tile_bytes` is not edited: other layers and the integration tests that call it directly (`tests/test_mvt_national_identity_probe_integration.py`) keep their behaviour.
- Order inside `_fetch_hydro_national_mvt_tile_bytes` when the identity is non-NULL:
  1. an explicit `_require_live_postgis_mvt(session, "hydro-national")` call, which issues no statement. `_fetch_postgis_tile_bytes` repeats the same gate later with the same outcome.
  2. the coverage helper;
  3. `_fetch_postgis_tile_bytes`.
- Consequences:
  - sqlite or disabled live PostGIS keeps its 424 byte-identically, including `details.layer_id = "hydro-national"`, with zero coverage statements.
  - A partially covered identity is refused before the expensive tile SQL runs.
- A cache hit never reaches the producer, so it costs no extra statement.
  - Consequence (accepted, #2031 scope): a partial tile cached before this change keeps being served on hit until its key rotates or retention removes it.
- An `ApiError` raised by the producer already propagates through `tile_generation_lock`, which unlinks its lock on exception (canonical `mvt-tile-cache-lifecycle` requirement), and writes no cache entry. This is the path the existing `source_identity_count <= 0` 424 uses.

### D3: three verdicts

| covered ∩ active | Verdict |
|---|---|
| covered = ∅ | fall through to the tile SQL. Today's 424 `MVT_LIVE_POSTGIS_UNAVAILABLE` stays byte-identical, and so do its display-v2 scenarios. |
| ∅ ≠ covered and covered ≠ active (strict subset, or a membership mismatch from the two-statement race) | 424 `MVT_NATIONAL_IDENTITY_INCOMPLETE` |
| covered = active (non-empty) | fall through; bytes, cache key and headers unchanged |

- Edge: active = ∅ (no active network at all). Without a race, covered is ∅ and the request falls through to today's 424. Under the two-statement race (a network activated between the statements), covered can be non-empty while active is ∅; the second row then refuses (fail-closed).
- Why falling through on covered = ∅ never yields a 200: the tile SQL's candidate runs (`latest_runs`, identity probe) are the coverage predicate plus the river-network-version join and the `valid_time` window, i.e. a subset of the coverage rows. Outside a race, an empty coverage set therefore always reaches the existing `source_identity_count <= 0` 424.
- Error body: `ApiError(status_code=424, code="MVT_NATIONAL_IDENTITY_INCOMPLETE", message=<says the identity is not covered by every active river network>, details={"layer_id": public_hydro_layer_id(variable) (i.e. "discharge", the same value the probe's 424 uses), "source", "cycle" (seconds-precision Z), "covered_network_count", "active_network_count"})`.
  - Counts only, never internal network ids: the route is public.
- Why 424 and a new code rather than a new status:
  - the route already declares 424, and MapLibre treats any non-2xx the same;
  - the issue requires distinguishability, which `error.code` provides;
  - no new status enters the contract.

### D4: OpenAPI — route-local response, shared one untouched

- Today `_patch_mvt_tile_openapi` points every MVT route's 424 at `components/responses/MvtLivePostgisUnavailable`, whose `error.code` enum is `[MVT_LIVE_POSTGIS_UNAVAILABLE]`.
- Widening that shared enum would document a code five routes never emit. Instead:
  - add `components/responses/MvtNationalIdentityUnavailable`, the same shape with enum `[MVT_LIVE_POSTGIS_UNAVAILABLE, MVT_NATIONAL_IDENTITY_INCOMPLETE]`;
  - point only the canonical source/cycle path's 424 at it;
  - mirror the change in `openapi/nhms.v1.yaml`, regenerate `apps/frontend/src/api/types.ts`, and keep `tests/test_openapi_drift.py` / `tests/test_api_contract.py` / `pnpm check:api-types` green.
- Tooling facts:
  - `openapi/nhms.v1.yaml` is hand-maintained.
  - `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` requires it to equal the runtime schema that `apps/api/openapi_patching.py` produces.
  - `apps/frontend/src/api/types.ts` is generated from the YAML (`pnpm generate:api`) and checked by `pnpm check:api-types`.
  - The route docstring is the operation description, so editing it requires the same edit in the YAML.
- Order of work: patcher (the `_patch_mvt_tile_openapi` loop points the canonical path at the new component) → hand-edit the YAML (the canonical path's 424 and `components/responses`) → `pnpm generate:api`.

### D5: known residuals (recorded, not fixed)

- **Instant-level partiality:** a fully covered cycle whose networks' coverage windows differ still renders a partial map at a `valid_time` outside the intersection window when requested by direct URL. The frontend only requests instants from the per-cycle valid-times list, which is clamped to the intersection.
- **Cached partial tiles** are served on hit until key rotation or retention (#2031). No `NATIONAL_DISCHARGE_QUERY_VERSION` bump.
- **Two READ COMMITTED statements:** the active set and the coverage rows are read in separate snapshots (#2087).
  - A set mismatch in either direction refuses, so the race fails closed.
  - A network activated between the statements can transiently refuse a cycle that is about to become complete. It is not cached, so the next miss re-evaluates.
- **Third snapshot:** the tile SQL runs in yet another statement after the helper. If a network's run stops being display-ready between the two, a partial 200 can still be produced and cached, which is the pre-change behaviour for that narrow window.
- **Cost:** one extra pair of coverage statements per cache miss for the canonical route, pinned to one cycle.
  - The node-27 cost is not measured here (D6); per-cycle binds keep it far below the 12-day `/cycles` intersection.

### D6: no node-27 live receipt in this PR

- The node-27 checkout runs API code at `a8db554d`. Master needs migration 000059 (narrow-store expand, #1986), so neither the production :8080 process nor an isolated master worktree can run against the production DB before that migration lands.
- The post-deploy receipt required by the #2153 acceptance criterion is tracked as a follow-up issue:
  - a listed cycle before and after, with identical bytes;
  - an `08-25T00Z`-type partial identity refused with the new code;
  - hit/miss latency.
- That follow-up depends on #2162's deployment window. Its side-by-side comparison (`mvt-tile-cache-lifecycle` receipt style) must use a fully covered cycle as the unchanged control, because a partial cycle differs by design.
- Not blocked by D6: real-DB integration tests on node-27 against a **throwaway** database (`tests/test_mvt_national_identity_probe_integration.py`, `national_tile` fixture, migrated from zero). They touch neither the production DB nor :8080, and they prove the helper SQL and the tile SQL agree on real rows (tasks E11).
