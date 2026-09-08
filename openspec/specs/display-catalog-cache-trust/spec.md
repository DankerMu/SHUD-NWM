# display-catalog-cache-trust Specification

## Purpose

Define who may force the display_readonly catalog cache (`apps/api/display_cache.py::display_catalog_cached`) to skip its store lookup and recompute: only the in-process warmer and a caller holding the configured token. Every other request, whatever headers it carries, is served by the TTL / stale-while-revalidate rules.
Also define what the cache admits and keeps: empty results are served but never stored nor replayed, both maps are bounded LRU (256, never cleared as a whole), the warmer replays at most 32 hit-ranked hot paths per tick, and `/api/v1/layers` caches the full per-`run_id` catalog and pages after the cache.
## Requirements
### Requirement: Forced refresh is granted only to the in-process warmer or a caller holding the configured token

`display_catalog_cached` SHALL bypass the store lookup and recompute the value only when `_force_refresh(request)` is true. `_force_refresh` SHALL return true when, and only when, either (a) the ASGI scope carries `scope["state"]["nhms_display_cache_warm"] is True`, which only the in-process warmer's `_replay_targets` sets by wrapping the application before handing it to `httpx.ASGITransport`, or (b) the runtime configuration holds a non-empty `display_cache_warm_token` (from `NHMS_DISPLAY_CACHE_WARM_TOKEN`, stripped; blank means unset) AND the request carries an `x-nhms-cache-warm` header whose value equals the token under `hmac.compare_digest` applied to the `encode("utf-8", "surrogateescape")` encodings of both strings (never to `str` objects, and with `surrogateescape` so a token that reached `os.environ` through an invalid UTF-8 byte cannot raise either: Starlette decodes header values as latin-1 and `compare_digest` raises `TypeError` on non-ASCII `str`). The runtime configuration is read from `request.app.state.runtime_config` with the same defensive attribute access as `_display_readonly`; a request object lacking `app`, `scope` or `headers`, a non-`str` header value, or a blank token SHALL yield false, and `_force_refresh` SHALL never raise. The literal value `refresh` SHALL NOT be privileged. When the token is unset, no header value SHALL force a refresh. The non-display-role passthrough (`loader()` called directly) SHALL be unchanged. `RuntimeConfig.public_dict()` and `repr(RuntimeConfig)` SHALL NOT include the token.

#### Scenario: An external request carrying the legacy header value hits the cache

- **WHEN** the display_readonly role has a cached value for key `k`, the token is unset, and a request carries `x-nhms-cache-warm: refresh`
- **THEN** `display_catalog_cached` returns the cached value and the loader is not called

#### Scenario: The configured token forces a recompute and the result is stored

- **WHEN** the runtime config on `request.app.state` holds `display_cache_warm_token == "abc"`, key `k` is cached, and a request carries `x-nhms-cache-warm: abc`
- **THEN** the loader is called, its value is stored, and a following plain request for `k` returns the new value
- **AND** a request carrying any other value (or no header) for `k` returns the cached value without calling the loader

#### Scenario: A non-ASCII header value is a cache hit, never an error

- **WHEN** the runtime config holds token `abc`, key `k` is cached, and a request carries `x-nhms-cache-warm` with the value `"ab\u00e9"` (a non-ASCII `str`, as Starlette produces for a byte ≥ 0x80)
- **THEN** `display_catalog_cached` returns the cached value without raising and the loader is not called

#### Scenario: The in-process warmer still refreshes without any token

- **WHEN** the token is unset, key `k` is cached from a plain request, and `_replay_targets(app, [path])` replays that path
- **THEN** the loader is called again and the store holds the replayed value

#### Scenario: The token is never exposed through the runtime config surface

- **WHEN** `load_runtime_config` is given `NHMS_DISPLAY_CACHE_WARM_TOKEN=" abc "`
- **THEN** `config.display_cache_warm_token == "abc"`, `config.public_dict()` has no key whose value is `"abc"`, and `repr(config)` does not contain `"abc"`; a missing or blank variable yields `None`

### Requirement: The node-27 prewarm sends the token when configured and degrades honestly when it is not

`scripts/node27_mvt_prewarm.py::fetch_json` SHALL send `x-nhms-cache-warm: <token>` when `NHMS_DISPLAY_CACHE_WARM_TOKEN` is set to a non-blank value in the process environment. When it is unset or blank, `fetch_json` SHALL NOT send the header, SHALL write exactly one warning line to stderr per process (`prewarm: NHMS_DISPLAY_CACHE_WARM_TOKEN unset; discovery may see up to 45 s stale catalog`), and SHALL otherwise behave unchanged (same URL, same `Accept`, same JSON parsing, same summary schema).

#### Scenario: Token present

- **WHEN** the environment holds `NHMS_DISPLAY_CACHE_WARM_TOKEN=abc` and `fetch_json` issues a discovery request
- **THEN** the request carries `x-nhms-cache-warm: abc`

#### Scenario: Token absent

- **WHEN** the variable is unset and `fetch_json` issues two discovery requests
- **THEN** neither request carries `x-nhms-cache-warm`, stderr holds exactly one warning line, and both responses are parsed normally

### Requirement: Cache admission and eviction never let a public request wipe the catalog cache

`display_catalog_cached` SHALL accept an optional keyword-only `cacheable: Callable[[Any], bool]` predicate; when the predicate is given and returns false for the loader's value, the value SHALL be returned to the caller but SHALL NOT be stored in `_store`, SHALL NOT be recorded in `_hot_paths`, and any existing `_store`/`_hot_paths` entry for that key SHALL be removed, on both the normal path and the forced-refresh path. `_store` and `_hot_paths` SHALL be bounded LRU maps of at most `_MAX_ENTRIES` (256) entries: a hit moves the key to the most-recently-used end, inserting beyond the bound evicts exactly the least-recently-used entry, and neither map is ever cleared as a whole except by the test hook `clear_display_catalog_cache`. Hot-path recording SHALL happen after the cache outcome is known and only for cacheable outcomes on the normal (non-forced) path, as `(path, last_access, hits)` with `hits` starting at 1 on a miss and incremented on every hit. The forced-refresh path SHALL NOT record or refresh `_hot_paths` (a warmer replay must not extend its own 1800 s active window; the window expires only through real client access); on that path the predicate only stores a cacheable value or, when false, triggers the removal above. `/api/v1/runs` SHALL pass a predicate that is true only when the page's `items` is non-empty; `/api/v1/layers/{layer_id}/valid-times` SHALL pass a predicate that is true only when `valid_times` is non-empty; `/api/v1/layers/discharge/cycles` SHALL pass no predicate. Client-controlled `str | None` key dimensions of `/api/v1/runs` (`basin_id`, `source`, `cycle_time`, `status`) and of `/api/v1/layers/{layer_id}/valid-times` (`run_id`, `source`, `cycle`) SHALL be interpolated with `!r`, so an absent dimension keeps its `None` spelling while a literal query value `None` cannot fold into the absent-dimension key or rewrite its hot path. Non-display roles SHALL keep calling the loader directly without evaluating the predicate.

#### Scenario: Non-cacheable keys neither evict nor become replay targets

- **WHEN** the display_readonly role has key `k` cached and 300 requests arrive for 300 distinct keys whose loader values fail the predicate
- **THEN** `k` is still in `_store`, none of the 300 keys is in `_store` or `_hot_paths`, and each of those loaders was called exactly once

#### Scenario: A burst of cacheable keys evicts one entry at a time, never the whole map

- **WHEN** 255 distinct cacheable keys are stored, key `k` is then hit, and 255 more distinct cacheable keys are stored
- **THEN** `k` is still in `_store`, `len(_store) == 256`, and the oldest of the first 255 keys has been evicted
- **AND** storing 300 distinct cacheable keys without touching `k` leaves `len(_store) == min(stored + 1, 256)` after every store (`k` plus the keys stored so far, capped at 256 — the map is never emptied) and `len(_store) == 256` at the end

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

#### Scenario: A literal `None` query value never folds into an absent-dimension key

- **WHEN** the display_readonly role has the unfiltered runs page cached and serves `GET /api/v1/runs?basin_id=None`, and has the national valid-times list cached and serves `GET /api/v1/layers/discharge/valid-times?run_id=None`
- **THEN** the runs request is answered from the store with the page for `basin_id == "None"` (an empty page, not cached), the unfiltered entry is still in `_store` and its hot path is still `/api/v1/runs`; the valid-times request is HTTP 404 and the national entry's hot path is unchanged

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

`/api/v1/layers` SHALL cache the complete layer catalog under the key `layers:{run_id!r}` (so the national catalog lives under `layers:None` while a literal `run_id=None` query value maps to `layers:'None'` and takes the cold path) and apply `[offset : offset + limit]` to the cached value. The sliced body SHALL be byte-identical to the previous loader-side slice for the same parameters. An `offset` at or beyond the catalog length SHALL yield HTTP 200 with `data: []` without a database query or a new cache entry; this contract SHALL be stated in the `offset` parameter's `description` in both the route's `Query(...)` and `openapi/nhms.v1.yaml`, and no `maximum` SHALL be added to `offset`.

#### Scenario: Three page requests, one catalog build

- **WHEN** the display_readonly role serves `/api/v1/layers?offset=0&limit=2`, `/api/v1/layers?offset=1&limit=1` and `/api/v1/layers?offset=5` against a three-layer catalog
- **THEN** the responses carry the first two layers, the second layer, and an empty `data` list with HTTP 200 respectively, the catalog builder ran once, and `_store` holds only the key `layers:None`

#### Scenario: The static OpenAPI document matches the runtime schema

- **WHEN** `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` runs
- **THEN** it passes with the `offset` description present on `/api/v1/layers` and the `/api/v1/runs` parameters unchanged

