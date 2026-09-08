# Design — mvt-budget-truncation-signal-2030

Fixture level: expanded (mandatory trigger: shared entrypoint — the final SELECT of `postgis_tile_sql` feeds
all five public tile layers, and `_fetch_postgis_tile_bytes` is the single production bind site).
Repair intensity: **medium**, not `high`, although a shared helper is touched: the change is observability-only —
bytes, status codes, binds and cache identity are unchanged and each is pinned in *Must preserve* with its own
oracle, so a second same-class finding (medium's escalation trigger) is the right sensitivity.
Project profile: NHMS (`openspec/project-profile.md`). Upstream suggested level: absent (hand-written
issue from a PR #2025 review deferral).

## Change surface

- `services/tiles/mvt.py::postgis_tile_sql` — final SELECT (currently ~`:1290-1300`, the block of
  `(SELECT … FROM prefilter_stats) AS …` projections): add
  `(SELECT intersecting_feature_count FROM prefilter_stats) AS intersecting_feature_count,` and
  `(SELECT intersecting_coordinate_count FROM prefilter_stats) AS intersecting_coordinate_count,`
  next to the other `prefilter_stats` projections. Nothing else in the statement moves.
- `apps/api/routes/hydro_display.py::_fetch_postgis_tile_bytes` (currently `:728-779`) — read four more
  columns (`intersecting_feature_count`, `intersecting_coordinate_count`,
  `feature_coordinate_overflow_count`, `coordinate_dimension_overflow_count`) with the same
  `int(row.get(...) or 0)` idiom, and emit the warning immediately before `return bytes(row["tile"] or b"")`.
- `apps/api/routes/hydro_display.py` — module-level `logger = logging.getLogger(__name__)`
  (`apps.api.routes.hydro_display` is a child of `apps.api`, so it inherits the stderr handler
  `apps/api/main.py::_install_api_log_handler` installs; no wiring needed).
- `tests/test_hydro_display_mvt_scaling.py` — `_budget_row` + new tests (see tasks.md).

## Must preserve

- Tile bytes for every layer/tile: the `tile` sub-select, every CTE, every bind and `ORDER BY` are untouched.
  Oracle: old-vs-new `postgis_tile_sql(layer)` text diff is exactly the two added lines for each of the five
  layers; node-27 md5(tile) equality on the sample in tasks 5.3.
- 413 / 424 / 500 semantics and their `details` payloads (existing tests
  `test_national_river_tile_above_its_own_limit_still_raises_413_against_that_limit`,
  `test_national_hydro_tile_keeps_the_shared_413_limit`, `test_per_basin_river_tile_keeps_the_shared_413_limit`
  stay green unchanged). The warning is emitted only on the path that returns bytes.
- Cache identity: `cache_key` does not hash SQL (`services/tiles/mvt.py:44-49`); bytes unchanged ⇒
  `NATIONAL_RIVER_NETWORK_QUERY_VERSION == "stream-type-aggregate-v3"` and
  `NATIONAL_DISCHARGE_QUERY_VERSION == "fair-network-budget-v5"` stay as pinned.
- `tests/river_ts_template_registry.py` golden: `sql_chains` is "blind to the SELECT list by construction"
  (`packages/common/river_ts_render.py:1875-1881`), so `GOLDEN_SHA256` and the fixture file MUST NOT change
  in this PR — a re-capture here is oracle weakening.
- Bind dictionary of `_postgis_tile_params` unchanged (exact-dict callers in tests and scripts).
- Stub rows that lack the new keys (older tests, scripts) read as `0` and stay silent: `0 > n` is false.

## Must add/change

- Four columns read by the route; two columns projected by the SQL.
- One WARNING record per truncated tile generation (not per request — cache hits never reach this code).
- Test lock that every `row.get("<col>")` key read in `_fetch_postgis_tile_bytes` is projected `AS <col>` by
  all five final SELECTs (guards the reverse regression: the route reading a column a layer stopped projecting;
  it would not have caught #2030 itself, whose defect was a column nobody read — that is what 3.1 pins).

## Decisions

- **D1 — Signal = WARNING log record, not a metrics counter.** The repo has no metrics/counter
  infrastructure (grep: no prometheus/statsd client under `apps/`); the display API already routes
  `apps.api.*` WARNING+ to `/tmp/display-api.log` via systemd `StandardError`. A greppable token
  `MVT_TILE_BUDGET_TRUNCATED` makes it a runtime capability (`grep -c` on the log) instead of a one-off
  SQL replay. Message is one line: `MVT_TILE_BUDGET_TRUNCATED layer_id=<id> z=<z> x=<x> y=<y>
  feature_count=<n>/<intersecting> max_features=<n> coordinate_count=<n>/<intersecting> max_coordinates=<n>`
  (the `/` pairs read "selected/intersecting"), plus the same fields in `extra=` for structured handlers.
- **D2 — Condition uses all four columns.** Overflow rows are removed by `preeligible` (window layers) /
  `eligible` (others) *before* `budget_stats`, so `intersecting > selected` also holds when a single
  feature overflows the per-feature coordinate limit or the dimension limit. Those two paths already
  produce an empty 200 (deferred sibling in the issue's Out-of-scope list); gating on
  `feature_coordinate_overflow_count == 0 AND coordinate_dimension_overflow_count == 0` keeps this warning
  meaning exactly "the fair budget window dropped rows". A tile with BOTH an overflow row AND budget
  truncation therefore stays silent — accepted, recorded as a non-goal.
- **D3 — Strict `>` on either axis.** `intersecting == selected` on both axes is the untruncated state and
  must be silent (node-27: all 516 `river-network-national` tiles today). The feature arm is not academic:
  the node-27 receipt found `hydro-national q_down` 3/6/3 truncated on exactly that arm in production
  (feature_count 10 000 / 10 991 against `:feature_limit` = `MVT_MAX_FEATURES`, coordinates 20 005 / 21 987
  well under 50 000), so a coordinate-only check would have stayed silent on the first real truncation.
- **D4 — Warning placement after the 424 check.** On a 424 (`source_identity_count <= 0`) there are no
  source rows, so `intersecting == 0 == selected` — nothing to warn about; on 413/500 the raise already
  carries the numbers. Emitting only on the bytes-returning path keeps one signal per outcome.
- **D5 — Missing columns read as zero, not as an error.** Same `row.get(...) or 0` idiom as the existing
  columns; a driver/dialect that drops a column degrades to silence rather than to a 500 on every tile.
  The column-coverage lock test (tasks 3.3) is what guards against the projection going missing again.
- **D6 — No `*_QUERY_VERSION` bump.** Bytes unchanged (Must preserve). Evidence: exact-two-line SQL diff
  outside the `tile` sub-select + node-27 md5 sample (tasks 5.3).
- **D7 — Live receipt runs in an isolated node-27 worktree**, never in the production checkout
  (`/home/nwm/NWM` at `5a86841c` serves `nhms-display-api.service`). The forced-limit case monkeypatches
  `collection_coordinate_limit` (or `_postgis_tile_params`) in-process and calls
  `_fetch_postgis_tile_bytes` directly, capturing the WARNING with `logging` — proving the *signal*, not
  just the SQL column.

## Seams under test

- `postgis_tile_sql(layer) -> str` for the five layers (text: projection present; structure: exact diff).
- `_fetch_postgis_tile_bytes(session, layer, params, *, z, x, y) -> bytes` with `_Session` stub rows
  and `caplog` at WARNING on logger `apps.api.routes.hydro_display`.
- Source-scan lock between `_fetch_postgis_tile_bytes`'s `row.get("...")` keys and each layer's final
  SELECT `AS <col>` set (route↔SQL column coverage).

## Selected risk packs

- Schema / columns / units / field names: new projected columns; names must match on both sides
  (tasks 3.2, 3.3).
- Legacy compatibility / examples: five layers, unchanged bytes and status codes; stub rows without the
  new keys stay silent (tasks 3.1, 3.4, 5.x).
- Error handling / rollback / partial outputs: no false positive on the two non-budget drop paths
  (tasks 3.1 negatives).
- Documentation / migration notes: receipt with the new SQL digests replacing the #2025 table (tasks 5.4).
- Domain — Published NHMS artifacts / display identity: cache key and query versions unchanged (D6).
- Domain — PostGIS / TimescaleDB domain behavior: the new columns are aggregates over `bounded_rows`
  already computed; node-27 run proves the projection executes on real PostGIS (tasks 5.2).

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — no route signature, OpenAPI or CLI change; the log line is
  not a public contract.
- Config / project setup: not selected — no env/config.
- File IO / path safety / overwrite: not selected — none.
- Auth / permissions / secrets: not selected — none; RO role suffices for the receipt.
- Concurrency / shared state / ordering: not selected — one warning per generation inside the existing
  generation lock; no shared state added.
- Resource limits / large input / discovery: not selected — no extra scan; `prefilter_stats` already
  aggregates `bounded_rows`.
- Release / packaging / dependency compatibility: not selected — stdlib `logging` only.

## Risk packs considered (domain, from `openspec/project-profile.md`)

- Geospatial / CRS / basin geometry: not selected — no geometry, CRS, `ST_Transform`, clipping or simplification change.
- Hydro-met time series / forcing windows: not selected — no `valid_time` / run-selection / coverage-window change.
- SHUD numerical runtime / conservation / NaN: not selected — display read path only.
- PostGIS / TimescaleDB domain behavior: selected (above).
- Slurm production lifecycle / mock-vs-real parity: not selected — no scheduler or compute surface.
- External hydro-met providers / snapshot reproducibility: not selected — no provider or ingest surface.
- Run manifest / QC provenance: not selected — no manifest, QC or receipt-schema surface touched.
- Published NHMS artifacts / display identity: selected (above).

## Non-goals

- Changing `MVT_MAX_COORDINATES`, `NATIONAL_RIVER_COLLECTION_COORDINATE_LIMIT`, `MVT_MAX_FEATURES`, the
  window ordering, or the 413 semantics.
- Frontend exposure of truncation (header/flag) — separate product decision.
- The empty-200 sibling: a per-feature coordinate/dimension overflow empties `budget_gate` and returns an
  empty tile with no signal; and a tile that has both an overflow row and budget truncation is silent
  under D2. Both stay out of scope (issue Out-of-scope; follow-up issue to be filed at Phase 8).
- Metrics/counter infrastructure.
- Any `*_QUERY_VERSION` bump or golden re-capture.

## Review focus

- The warning condition and its placement (D2/D3/D4) — false positives on overflow paths, false negatives
  on equal counts.
- Exact-two-line SQL diff per layer; nothing inside the `tile` sub-select or any CTE moved.
- The column-coverage lock actually reads the route source and all five SQL texts (not a hard-coded list).
- Existing 413 tests unchanged and green; `_budget_row` defaults keep them meaningful.
