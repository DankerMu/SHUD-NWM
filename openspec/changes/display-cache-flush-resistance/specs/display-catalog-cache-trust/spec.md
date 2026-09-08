# Spec: display-catalog-cache-trust

## ADDED Requirements

### Requirement: Cache admission and eviction never let a public request wipe the catalog cache

`display_catalog_cached` SHALL accept an optional keyword-only `cacheable: Callable[[Any], bool]` predicate; when the predicate is given and returns false for the loader's value, the value SHALL be returned to the caller but SHALL NOT be stored in `_store`, SHALL NOT be recorded in `_hot_paths`, and any existing `_store`/`_hot_paths` entry for that key SHALL be removed, on both the normal path and the forced-refresh path. `_store` and `_hot_paths` SHALL be bounded LRU maps of at most `_MAX_ENTRIES` (256) entries: a hit moves the key to the most-recently-used end, inserting beyond the bound evicts exactly the least-recently-used entry, and neither map is ever cleared as a whole except by the test hook `clear_display_catalog_cache`. Hot-path recording SHALL happen after the cache outcome is known and only for cacheable outcomes on the normal (non-forced) path, as `(path, last_access, hits)` with `hits` starting at 1 on a miss and incremented on every hit. The forced-refresh path SHALL NOT record or refresh `_hot_paths` (a warmer replay must not extend its own 1800 s active window; the window expires only through real client access); on that path the predicate only stores a cacheable value or, when false, triggers the removal above. `/api/v1/runs` SHALL pass a predicate that is true only when the page's `items` is non-empty; `/api/v1/layers/{layer_id}/valid-times` SHALL pass a predicate that is true only when `valid_times` is non-empty; `/api/v1/layers/discharge/cycles` SHALL pass no predicate. Non-display roles SHALL keep calling the loader directly without evaluating the predicate.

#### Scenario: Non-cacheable keys neither evict nor become replay targets

- **WHEN** the display_readonly role has key `k` cached and 300 requests arrive for 300 distinct keys whose loader values fail the predicate
- **THEN** `k` is still in `_store`, none of the 300 keys is in `_store` or `_hot_paths`, and each of those loaders was called exactly once

#### Scenario: A burst of cacheable keys evicts one entry at a time, never the whole map

- **WHEN** 255 distinct cacheable keys are stored, key `k` is then hit, and 255 more distinct cacheable keys are stored
- **THEN** `k` is still in `_store`, `len(_store) == 256`, and the oldest of the first 255 keys has been evicted
- **AND** storing 300 distinct cacheable keys without touching `k` leaves `len(_store) == 256` at every step (the map is never emptied)

#### Scenario: A forced refresh never touches the hot-path table

- **WHEN** key `k` is hot with `(path, t0, hits)` and a warm-scope replay of its path makes the loader return a cacheable value
- **THEN** `_store` holds the new value and `_hot_paths[k]` is unchanged (same `last_access` `t0`, same `hits`)

#### Scenario: A forced refresh that yields a non-cacheable value forgets the key

- **WHEN** key `k` is cached and hot, and an in-process warm-scope replay of its path makes the loader return a value failing the predicate
- **THEN** `k` is removed from `_store` and `_hot_paths`, and the next plain request for `k` calls the loader instead of returning the old value

#### Scenario: An empty runs page is served but not cached

- **WHEN** the display_readonly role serves `GET /api/v1/runs?basin_id=<unknown>&limit=1` and the store returns `items: []`
- **THEN** the response is HTTP 200 with the same body as before this change, the key is absent from `_store` and `_hot_paths`, and a second identical request calls the store again
- **AND** a request whose page has items is cached and a second identical request does not call the store

#### Scenario: An empty valid-times list is served but not cached

- **WHEN** the display_readonly role serves `GET /api/v1/layers/discharge/valid-times?source=gfs&cycle=<cycle outside the coverage intersection>`
- **THEN** the response is HTTP 200 with `valid_times: []` and the key is absent from `_store` and `_hot_paths`, while a cycle with coverage is cached

### Requirement: The warmer replays a bounded, hit-ranked subset of hot paths per tick

Each warmer tick SHALL replay at most `DISPLAY_CATALOG_WARM_REPLAY_MAX` (32) hot paths, chosen from the entries whose `last_access` lies within `DISPLAY_CATALOG_WARM_ACTIVE_WINDOW_SECONDS`, ordered by `hits` descending then `last_access` descending. The selection SHALL be taken as a snapshot under `_lock` and the lock SHALL be released before any replay, keeping the `stop_display_catalog_warmer` join contract unchanged.

#### Scenario: Forty hot paths, thirty-two replayed, the most-hit first

- **WHEN** `_hot_paths` holds 40 active entries, one with `hits == 5` and the others with `hits == 1`
- **THEN** the tick hands `_replay_targets` exactly 32 paths and the first one is the `hits == 5` path
- **AND** with `DISPLAY_CATALOG_WARM_REPLAY_MAX` patched to 2 the tick hands over exactly 2 paths

### Requirement: `/api/v1/layers` pagination is applied after the cache and out-of-range offsets yield an empty page

`/api/v1/layers` SHALL cache the complete layer catalog under the key `layers:{run_id}` and apply `[offset : offset + limit]` to the cached value. The sliced body SHALL be byte-identical to the previous loader-side slice for the same parameters. An `offset` at or beyond the catalog length SHALL yield HTTP 200 with `data: []` without a database query or a new cache entry; this contract SHALL be stated in the `offset` parameter's `description` in both the route's `Query(...)` and `openapi/nhms.v1.yaml`, and no `maximum` SHALL be added to `offset`.

#### Scenario: Three page requests, one catalog build

- **WHEN** the display_readonly role serves `/api/v1/layers?offset=0&limit=2`, `/api/v1/layers?offset=1&limit=1` and `/api/v1/layers?offset=5` against a three-layer catalog
- **THEN** the responses carry the first two layers, the second layer, and an empty `data` list with HTTP 200 respectively, the catalog builder ran once, and `_store` holds only the key `layers:None`

#### Scenario: The static OpenAPI document matches the runtime schema

- **WHEN** `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` runs
- **THEN** it passes with the `offset` description present on `/api/v1/layers` and the `/api/v1/runs` parameters unchanged
