# Invariant Matrix — I4 / issue #2007 (hydro-national {source}/{cycle})

Per-issue fixture addendum (repair intensity `high`). Kept as its own file, not inlined into the
shared `tasks.md`, because 14 issues write that file concurrently.

Governing invariant: a `hydro-national` tile's bytes, its cache entry, and its 200/424 verdict are
determined entirely by the requested identity `(source, cycle, variable, valid_time, z/x/y)`; when the
requested `(source, cycle)` has no display-ready run the route MUST answer 424, never an empty 200; and
the legacy source-less route keeps its pre-change run selection, response bytes, and 200/424 verdict.
The legacy route's *cache key* deliberately rotates once, because task 3.1 bumps
`NATIONAL_DISCHARGE_QUERY_VERSION` to `-v5` and that value feeds `national_discharge_source_version`,
which the legacy route embeds in its `source_version`. One post-deploy cold miss per legacy tile is
expected and must not be read as a regression in the node-27 cold/hot numbers.

Source-of-truth identity/contract: the pair `(lower(hydro.hydro_run.source_id), hydro.hydro_run.cycle_time)`
bound as `:source` / `:cycle`, spelled through `canonical_mvt_time` (`YYYY-MM-DDTHH:MM:SSZ`); cache identity is
`cache_key(TileInput)` over `source_version` + `variant_id` + `valid_time`.

## Decided here (the fixture left these open)

1. **Legacy pass-through form.** The two predicate sites are NULL-guarded and the legacy route binds
   `:source`/`:cycle` as `None`:

   ```sql
   AND (CAST(:source AS text) IS NULL OR lower(h.source_id) = :source)
   AND (CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time = :cycle)
   ```

   `CAST(...)`, never `:source::text`: SQLAlchemy's `text()` bind regex backtracks across a following
   `::` and emits a bogus extra bind (measured on this repo's SQLAlchemy 2.0.49:
   `:source::text` yields binds `['sourc', 'source']`; the `CAST` form yields `['source']`). The bogus
   bind passes every fake-session test and only fails against a real driver.
   Consequence for the shape assertion: the literal contiguous string
   `lower(h.source_id) = :source AND h.cycle_time = :cycle` appears ZERO times. This decision
   **supersedes** issue #2007's acceptance-criterion wording, and the AC bullet has been amended to match.

   **Correction (post cross-review).** An earlier version of this file justified the supersession by
   claiming no guard form could satisfy both the contiguous-string AC and an unchanged legacy route.
   That claim was FALSE. This form satisfies both, keeps the contiguous string verbatim, declares
   exactly the binds `['cycle','source']`, and preserves legacy pass-through when both are NULL:

   ```sql
   AND ((CAST(:source AS text) IS NULL AND CAST(:cycle AS timestamptz) IS NULL)
        OR (lower(h.source_id) = :source AND h.cycle_time = :cycle))
   ```

   The real and sufficient justification is that the assertion chosen is **strictly stronger**: locating
   each sub-predicate separately at each of the two sites, plus asserting the NULL guard, plus asserting
   no `:source::text`-style phantom bind, rejects the half-bound implementation that a contiguous-string
   match satisfies whenever both occurrences land in the data CTE — which is exactly the bug D1 warns
   about. The supersession was a deliberate trade, not a forced move.
2. **Sub-second instants.** `canonical_mvt_time` does not truncate: a datetime with non-zero
   microseconds round-trips as `...:00.500000Z`. The new route therefore accepts the zero-microsecond
   spellings the contract is written for (`...T12:00:00.000Z`, `...T12:00:00+00:00`, `...T12:00:00Z`)
   and rejects a non-zero-microsecond `cycle`/`valid_time` with 422. Silent truncation is not chosen:
   it would serve the `12:00:00` tile under a `12:00:00.500Z` request. Precision of the claim: pydantic
   truncates below microsecond resolution before this check sees the value, so a spelling with seven or
   more fractional digits whose microsecond part is zero (`...:00.0000001Z`) is accepted and collapses
   onto `12:00:00`. The aliasing window is one microsecond, no producer emits that, and both spellings
   name the same identity — but the rule enforced is "non-zero MICROSECOND", not "no fractional part".
3. **ETag is not part of the identity contract.** `stable_etag(data)` hashes tile bytes only
   (`services/tiles/mvt.py:231-232`); `source_version` reaches `cache_key`, not the ETag. The
   identity assertion is therefore on `cache_key` / the `X-Tile-Cache-Key` response header. Two
   different `(source, cycle)` tiles having different ETags is a content observation, not an
   invariant. `stable_etag` and `build_raw_tile_response` are shared by all five tile layers and are
   OUT of this PR's boundary — do not touch them.

## Surfaces

- Producers: `services/tiles/mvt.py::postgis_tile_sql("hydro-national")` — the `latest_runs` CTE and the
  `source_identity_stats_sql` probe's inline `SELECT DISTINCT ON (mi.river_network_version_id)` sub-select;
  `national_discharge_source_version`.
- Validators/preflight: `apps/api/routes/hydro_display.py` new route's `source` enum + `cycle` RFC3339 parse,
  the non-zero-microsecond rejection, `_validate_supported_hydro_variable`, `validate_xyz`.
- Storage/cache/query: `cache_key(TileInput)`, `_file_cache_path`, DB tile-cache read/upsert (`source_version`).
- Public routes/entrypoints: new 7-segment national route; legacy 5-segment national route (alias, unchanged
  behavior — its fetch helper does change, because `text()` raises on a missing named bind).
- Frontend/downstream consumers: none in this PR — the `/api/v1/layers` discharge catalog entry stays on the
  legacy template (I5/#2009); `apps/frontend/src/api/types.ts` is refreshed only by `pnpm generate:api`.
- Failure paths/rollback/stale state: the `source_identity_count <= 0` 424 branch in `_fetch_postgis_tile_bytes`
  vs. the same-code 424 from `_require_live_postgis_mvt` (told apart by `details`).
- Evidence/audit/readiness: `apps/api/openapi_patching.py::_patch_mvt_tile_openapi` `mvt_paths`,
  `openapi/nhms.v1.yaml`, `tests/test_openapi_drift.py`, `tests/test_display_publish_status_only.py`
  (counts the display-ready status predicate exactly twice inside `postgis_tile_sql`),
  `tests/test_mvt_national_identity_probe_integration.py` (throwaway-DB oracle; opt-in via
  `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=...`, skipped in the PR lane, so it must be run
  explicitly on node-27), node-27 live receipt.

## Regression rows

- New route, `(gfs, cycle)` with a display-ready run -> 200, non-empty PBF.
  Evidence: integration test + node-27 receipt.
- New route, `(ifs, same cycle)` with no run while gfs has one -> 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`, `details`
  carries z/x/y and NOT `required_env`. Evidence: integration test (deterministic seed; live data currently has
  both sources complete at every cycle, so the receipt must use a cycle with no runs at all and say so).
- New route, path `ifs` against a run stored as `source_id = 'IFS'` -> 200 (case-insensitive `lower()` match).
  Production stores exactly `gfs` and `IFS`, verified on node-27. Evidence: integration test.
- New route, `cycle=...T12:00:00.000Z` / `...T12:00:00+00:00` / `...T12:00:00Z` -> same bound `:cycle` value and
  the same `cache_key`; two different `(source, cycle)` pairs -> different `cache_key`. Evidence: unit test.
- New route, non-zero-microsecond `cycle` -> 422. Evidence: unit test.
- New route, `source=ERA5` / `source=best` / non-RFC3339 `cycle` -> 422 with no tile SQL executed (session
  override whose `execute` raises). Evidence: unit test.
- Legacy 5-segment route -> unchanged run selection, bytes and 200/424 verdict with `:source`/`:cycle` bound
  NULL. Evidence: the three existing cases in `tests/test_mvt_national_identity_probe_integration.py`, which
  drive the legacy route, must stay green unchanged.
- Unchanged sibling layers: `postgis_tile_sql("hydro")` and `postgis_tile_sql("river-network-national")` contain
  no `:source`/`:cycle` bind. Evidence: unit test.
- `national_discharge_source_version(session)` called with no arguments emits SQL that still satisfies the
  existing assertions at `tests/test_hydro_display_mvt_scaling.py:62-68`. Evidence: that test, unchanged.
- Runtime `app.openapi()` for the new path -> has the 424 response, the `q_down` variable enum, and z/x/y
  `maximum` 14/16383, and equals the hand-written `openapi/nhms.v1.yaml` entry. Evidence: new unit test +
  `tests/test_openapi_drift.py`.

## Mutation matrix

The governing invariant of this surface, learned the expensive way across three review rounds:
**every behavioral claim must have a red-capable oracle. A predicate that is present in the SQL string
but ineffective, or an argument that is passed but unasserted, must make at least one test fail.
String-shape assertions are not oracles.** Rounds 1–3 each closed exactly the holes named and left the
neighbours open (measured on node-27: deleting every `:cycle` predicate → 5 passed; deleting the
identity pair from the data CTE only → 5 passed; the route dropping `(source, cycle)` on the digest
call → 84 passed; the legacy route binding `source="gfs"` → 109 + 7 passed; `AND (` → `OR  (` in the
digest → 109 + 7 passed; deleting `validate_xyz` → 109 passed; narrowing the RFC3339 offset class to
`\+` → 109 passed). This table is the enumeration that replaces that pattern; extend it rather than
re-derive it.

`local` = `uv run pytest tests/test_hydro_display_mvt_scaling.py tests/test_api_contract.py
tests/test_openapi_drift.py tests/test_openapi_31_contract.py tests/test_display_publish_status_only.py -q`
(126 passed as of this row set; 117 before it). `node-27` =
`NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... TMPDIR=/home/nwm/tmp uv run pytest
tests/test_mvt_national_identity_probe_integration.py tests/test_river_ts_read_path_surrogate_keys_integration.py -q`
(skipped locally — no PostgreSQL on the dev Mac).

|#|Claim|Mutation (file:line, before → after)|Test that goes red|Where|
|---|---|---|---|---|
|1|The bound `(source, cycle)` selects its own run, not the newest one|`services/tiles/mvt.py:771-772` — delete both predicates from the `latest_runs` CTE|`test_national_identity_tile_serves_the_requested_source_not_the_other_one_at_that_cycle` (wrong run paints the tile at 200)|node-27|
|2|The identity PROBE decides 424 on the same identity, so another source's run cannot answer "present"|`services/tiles/mvt.py:729-730` — delete both predicates from `source_identity_stats`' run-discovery sub-select|`test_national_identity_tile_is_424_for_the_source_without_a_run_at_that_cycle` (empty 200 instead of 424)|node-27|
|3|…and its `:cycle` half specifically|`services/tiles/mvt.py:730` — delete the `:cycle` line from the probe only|`test_national_identity_tile_serves_the_requested_cycle_not_the_newest_one` (`_PRUNED_CYCLE_TIME` → empty 200); locally only the shape assertion in `test_national_tile_sql_binds_source_and_cycle_at_both_run_selection_sites`|node-27 (behavioral) / local (shape)|
|4|The predicates are CONJUNCTS at all three sites|`services/tiles/mvt.py:729/771/1563` — `AND (` → `OR  (` (SQL's AND binds tighter, so the row set reverts to unbound)|`test_national_digest_narrows_the_ranked_runs_to_the_bound_identity` (all four digests collapse); locally `test_national_tile_sql_binds_source_and_cycle_at_both_run_selection_sites` + `test_national_digest_narrows_to_the_requested_identity_and_stays_null_without_one` (shape)|node-27 (behavioral) / local (shape)|
|5|`lower(h.source_id)`: path `ifs` matches a run stored `IFS`|`services/tiles/mvt.py:729/771/1563` — `lower(h.source_id) = :source` → `h.source_id = :source`|`test_national_identity_tile_matches_an_uppercase_source_id_from_a_lowercase_path` (424 for every IFS tile); locally shape only|node-27 (behavioral) / local (shape)|
|6|`h.cycle_time = :cycle`, an EQUALITY, not a bound|`services/tiles/mvt.py:730/772/1564` — `=` → `<=`|`test_national_identity_tile_serves_the_requested_cycle_not_the_newest_one` (`_UNLANDED_CYCLE_TIME`, a not-yet-issued cycle, gets painted by the newest run at 200); locally shape only. `_PRUNED_CYCLE_TIME` alone cannot see this — it is older than every run, so `<=` also selects nothing there|node-27 (behavioral) / local (shape)|
|7|The digest's ranked sub-query narrows to the identity, so a re-run of a non-latest identity rotates the cache key|`services/tiles/mvt.py:1563-1564` — delete both predicates|`test_national_digest_narrows_the_ranked_runs_to_the_bound_identity`|node-27|
|8|…and the unbound call keeps answering the OVERALL-latest question|same row 7 mutation, inverted: make `source`/`cycle` non-optional|`test_national_digest_narrows_the_ranked_runs_to_the_bound_identity` (`unbound == late`) + every legacy/catalog caller (making the kwargs required is a local `TypeError` at the legacy and catalog call sites, so this row is decided without a database)|local|
|9|Both routes hand the digest helper their own identity|`hydro_display.py:421` — drop the kwargs|`test_each_national_route_hands_the_digest_helper_its_own_identity`|local|
|10|The digest VALUE reaches `cache_key`|`hydro_display.py:1161` — drop `:{source_digest}` from `source_version`|`test_national_identity_cache_key_moves_when_the_identity_digest_moves`|local (measured: 1 failed / 125 passed)|
|11|The legacy alias binds NULL, not a source|`hydro_display.py:472` — `source=None` → `source="gfs"`|`test_every_national_tile_sql_bind_is_supplied_by_the_route_that_executes_it[legacy]` (bind VALUES, not just the key set) + `test_national_identity_tile_matches_an_uppercase_source_id_from_a_lowercase_path`'s legacy request at `_IFS_WINDOW_END`, the one instant no gfs run covers|local (measured: 1 failed / 125 passed) + node-27|
|12|The NULL guard is what keeps the legacy alias's run selection, bytes and 200/424 verdict unchanged|`services/tiles/mvt.py:771` and `:729` — delete the `CAST(:source AS text) IS NULL OR` guard including its trailing space (and the `:cycle` twin), leaving a bare `lower(h.source_id) = :source`; the legacy route binds NULL, so the predicate becomes `NULL = NULL` and selects nothing|the three pre-#2007 legacy cases (`..._is_424_when_no_display_ready_run_covers_the_instant`, `..._is_424_on_an_interior_coverage_window_gap`, `..._is_200_with_a_non_empty_mvt_when_the_instant_has_data`) plus `_assert_tile_was_painted_by(_request_tile(...), _LATE_GFS_RUN_ID, _RUN_ID)` — the 200 cases turn 424|node-27|
|13|The legacy alias's ACCEPT-SET is unchanged (no RFC3339 shape gate)|`hydro_display.py:446` — `valid_time: datetime` → `valid_time: Rfc3339Instant`|`test_legacy_national_route_keeps_accepting_the_instant_spellings_it_always_did`|local (measured: 1 failed / 125 passed)|
|14|`{source}/{cycle}` separate two cache identities|`hydro_display.py:1161` — drop `{source}` or `{cycle_text}`|`test_national_identity_route_gives_two_identities_two_cache_keys`|local|
|15|`Z`, `.000Z`, `+00:00`, `+08:00`, `-08:00` are ONE instant, one bind, one cache key|`hydro_display.py:117` — `(Z\|[+-]\d{2}:\d{2})$` → `(Z\|\+\d{2}:\d{2})$`|`test_national_identity_route_collapses_time_spellings_onto_one_bind_and_one_cache_key` (the `-08:00` spelling, added for this row)|local (measured: 1 failed / 125 passed)|
|16|A bare epoch / offset-less / space-separated `cycle` or `valid_time` is 422, never coerced into SQL|`hydro_display.py:117` — delete the regex gate|`test_national_identity_route_rejects_a_bad_identity_before_running_any_sql` (6 rows)|local|
|17|Sub-second and out-of-UTC-range instants are 422, not truncation and not a 500|`hydro_display.py:1118` / `:1127` — drop the `microsecond` check / the `OverflowError` guard|same test (4 rows)|local|
|18|A bad `variable` costs no SQL|`hydro_display.py:409` — delete `_validate_supported_hydro_variable(variable)`|`test_national_identity_route_rejects_a_bad_identity_before_running_any_sql[.../q_up/...]`|local (measured: 1 failed / 125 passed)|
|19|A bad `z`/`x`/`y` costs no SQL|`hydro_display.py:410` — delete `validate_xyz(z, x, y)`|`test_national_identity_route_rejects_bad_tile_coordinates_before_running_any_sql` (4 rows)|local (measured: 4 failed / 122 passed)|
|20|`GET /api/v1/layers`' digest call stays identity-free (I5/#2009's boundary)|`hydro_display.py:262` — `national_discharge_source_version(session, source="gfs")`|`test_layer_catalog_still_digests_every_source_not_one_identity`|local (measured: 1 failed / 125 passed)|
|21|The new path is in `mvt_paths`, so the runtime schema carries the 424, the `q_down` enum and the z/x/y maxima|`apps/api/openapi_patching.py:341` — delete the tuple entry|`test_runtime_openapi_documents_the_national_identity_tile_route` + `test_static_openapi_matches_runtime_schema`|local (measured: 2 failed / 124 passed)|
|22|Sibling layers carry no `:source`/`:cycle`|add either bind to `postgis_tile_sql("hydro")`|`test_sibling_tile_layers_carry_no_source_or_cycle_bind`|local|
|23|Every bind the SQL declares is supplied by both call sites (`text()` raises at execution time otherwise)|add a bind to the SQL without a matching param|`test_every_national_tile_sql_bind_is_supplied_by_the_route_that_executes_it` (both routes)|local|
|24|`NATIONAL_DISCHARGE_QUERY_VERSION` is `fair-network-budget-v5`, so the legacy cache key rotates once on deploy|`services/tiles/mvt.py:63` — revert to `-v4`|`test_national_discharge_query_version_is_pinned_to_the_literal_the_spec_names`|local|
|25|No `:source::text`-style phantom bind (SQLAlchemy 2.0.49 emits `['sourc','source']`)|`CAST(:source AS text)` → `:source::text`|`test_national_tile_sql_binds_source_and_cycle_at_both_run_selection_sites` (`assert not binds & {"sourc","cycl"}`)|local|
|26|The digest adds no second `h.status IN (...)`|add one|`tests/test_display_publish_status_only.py:191/193/196`|local|
|27|`valid_time` reaches `cache_key`, so two instants of one `(source, cycle)` are two cache entries|`hydro_display.py:1162` — `valid_time=_format_time(valid_time),` → `valid_time="1970-01-01T00:00:00Z",`|`test_national_identity_route_gives_two_identities_two_cache_keys` (the `valid_time=...T06:00:00Z` case collapses onto the base)|local (measured: 1 failed / 125 passed)|
|28|`z` reaches its own `TileInput` slot, so `(z=4, x=13, y=6)` and `(z=5, x=13, y=6)` are two cache entries|`hydro_display.py:1163` — `z=z,` → `z=x,`|same test (the `z=5` case becomes the base's twin at `z=13`)|local (measured: 1 failed / 125 passed)|
|29|`x` reaches its own slot (not `y`'s value), so `(4,13,6)` and `(4,6,6)` are two cache entries|`hydro_display.py:1164` — `x=x,` → `x=y,`|same test (the `x=6` case becomes the base's twin at `x=6`)|local (measured: 1 failed / 125 passed)|
|30|`y` reaches its own slot (not `x`'s value), so `(4,13,6)` and `(4,13,13)` are two cache entries — the swap would serve the second requester the first one's bytes, and the file cache has no TTL|`hydro_display.py:1165` — `y=y,` → `y=x,`|same test (the `y=13` case becomes the base's twin at `y=13`)|local (measured: 1 failed / 125 passed)|

### Rows with NO oracle (recorded, not closed)

- `validate_identifier(variable, "variable")` at `hydro_display.py:408` is **behaviorally unreachable**
  on this route: `SUPPORTED_HYDRO_MVT_VARIABLES == ("q_down",)` and `q_down` satisfies
  `SAFE_TILE_IDENTIFIER_RE`, so every shape-invalid spelling is also unsupported and
  `_validate_supported_hydro_variable` is always the layer that rejects. Deleting the line leaves the
  suite green (measured: 126 passed). It is defence in depth, spelled identically on the four sibling
  tile routes, and the only way to distinguish it would be to assert `error.details`, which the route's
  stated contract ("one rejection contract for the whole route, whichever layer rejects") forbids.
  Left in place, not fixed.
- `_require_seconds_precision_instant`'s `value.replace(tzinfo=UTC)` branch
  (`hydro_display.py:1126`) is unreachable from the new route: `_RFC3339_INSTANT_RE` requires an
  offset, so `value.tzinfo` is never `None`. Reachable only if a future caller passes a naive
  datetime. Defensive, no oracle possible without a production change.
- The comment claim that a `.500` fractional part "gets the precision message rather than a shape one"
  has no oracle by design: both render 422 `VALIDATION_ERROR` and the route's contract is that they are
  indistinguishable to a client. Only the `.000` half is behavioral, and it is pinned by row 15.
