## Why

- #2153: the canonical national discharge tile route `GET /api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (public, unauthenticated) accepts any RFC3339 `cycle`.
  - When only some active river networks have a display-ready run for that `(source, cycle)`, the route returns 200 with a national map that paints only those networks, and caches it.
  - Such a tile is visually indistinguishable from "these basins have no water". The fail-closed rule behind `national_discharge_cycles` and `national_discharge_valid_times` exists to prevent exactly that misreading.
  - node-27 measurement: `gfs 2026-08-25T00Z` returned 200 with 1 183 693 bytes, against 1 374 286 bytes for a fully covered cycle (`docs/runbooks/receipts/2026-09-08-issue-2032-mvt-cache-measurement-node27.md:104-122`).
- Owner ruling (2026-09-14, option a′ from the #2153 discussion) — refuse **partially covered** identities using the same rule the per-cycle `/api/v1/layers/discharge/valid-times` branch applies (the set of networks covering `(source, cycle)` equals the set of active networks). No 12-day lookback.
  - A cycle that every network covers keeps rendering regardless of age.
  - The frontend never requests a partially covered pair: its per-cycle valid-times answer is `[]` and the layer is disabled (#2131).
  - Direct URL access is therefore the only path that changes.

## What Changes

- The canonical source/cycle national tile route refuses a `(source, cycle)` identity when some, but not all, active river networks cover it:
  - HTTP 424 with the new error code `MVT_NATIONAL_IDENTITY_INCOMPLETE`;
  - `details` carries the covered and active network **counts**.
- Unchanged:
  - an identity no active network covers keeps today's HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE` (display-v2 "One source has a run and the other does not");
  - a fully covered identity keeps today's bytes, cache key and headers.
- The check runs only on a tile-cache miss, after the live-PostGIS gate and before the tile SQL. A cache hit costs no extra statement.
- The coverage predicate is extracted into one helper in `services/tiles/mvt.py`, used by both the per-cycle valid-times branch and the tile route, so the two surfaces cannot drift.
- OpenAPI: the canonical route's 424 response documents both codes. The other MVT routes keep the existing single-code response.

## Non-goals

- `national_discharge_cycles`, its 12-day lookback and its response shape.
- The legacy `/api/v1/tiles/hydro-national/{variable}/{valid_time}/...` route: mixed-cycle by design, `source`/`cycle` are NULL.
- Purging already-cached partial tiles, or bumping `NATIONAL_DISCHARGE_QUERY_VERSION` to purge them (cold-misses every national tile; #2031 owns cache self-healing).
- Instant-level partiality: a `valid_time` outside the per-cycle intersection window of a fully covered cycle (design D5 residual).
- The two-statement READ COMMITTED window (#2087).
- Frontend changes; a coverage response header (option b, rejected: no client reads tile headers).
- node-27 deployment and live receipt (design D6).

## Impact

- Code: `services/tiles/mvt.py` (helper), `apps/api/routes/hydro_display.py` (national fetch), `apps/api/openapi_patching.py`, `openapi/nhms.v1.yaml`, `apps/frontend/src/api/types.ts` (regenerated types only), `tests/test_hydro_display_mvt_scaling.py`, `tests/test_mvt_national_identity_probe_integration.py` (plus the OpenAPI drift/contract tests if their fixtures pin the 424 schema).
- Specs: one ADDED requirement in `mvt-tile-contract`.
