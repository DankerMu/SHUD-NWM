# Invariant Matrix — I5 / issue #2009 (discharge cycles catalog + per-cycle valid times)

Per-issue fixture addendum (fixture level `expanded`, repair intensity `high`). Kept as its own file,
not inlined into the shared `tasks.md`, because 14 issues write that file concurrently. Sibling
precedent: `invariant-matrix-i4-2007.md` (#2007, merged) — its decisions about instant spelling,
`cache_key` vs ETag, and the mutation-matrix discipline are inherited, not restated.

Governing invariant: **the `/api/v1/layers` `discharge` entry advertises exactly one national
identity — `(default_source, default_cycle)` — and every instant it advertises is covered by a
display-ready run in *every* active river network for that identity.** The catalog entry, the
`cycles` endpoint and the `valid-times` endpoint MUST all derive from that same intersection, and
when the intersection is empty they MUST say so (`default_cycle = null`, `valid_times = []`,
`cycles = []`) rather than degrade to a partial or single-basin answer. The entry's identity is
independent of `run_id`: runless and `?run_id=<X>` responses are byte-identical for `discharge`.

Source-of-truth identity/contract: `(lower(hydro.hydro_run.source_id), hydro.hydro_run.cycle_time)`
joined to `hydro.run_display_coverage` with `segment_count > 0`, restricted to
`core.model_instance.active_flag AND river_network_version_id IS NOT NULL`; every instant spelled
through `canonical_mvt_time` (`YYYY-MM-DDTHH:MM:SSZ`, seconds precision, literal `Z`).

## Decided here (the fixture and issue left these open)

1. **One SQL, one status predicate, no oracle weakening.**
   `tests/test_display_publish_status_only.py::test_national_digest_membership_shares_one_status_set_with_the_data_side_queries`
   pins `h.status IN ('succeeded', 'parsed', 'published')` at four counts:
   `tile_sql == 2` (`:187`), `digest == 1` (`:191`), the `def national_discharge_valid_times` →
   `def _valid_time_discovery` slice `== 1` (`:192`), and the module `== 5` (`:193`).
   (tasks.md 3.1 quotes an older `:191/193/196` triple for the same assertions; the line numbers above
   are the ones verified against this branch's HEAD — do not "correct" them back.)
   A second national query would move two of them. Therefore the cycle/coverage SQL is factored into
   **one** private helper `_national_discharge_coverage_rows(session, *, source=None, cycle=None)`
   which is the sole owner of that predicate for the national discovery path; both
   `national_discharge_valid_times` and `national_discharge_cycles` consume it. All four pinned counts
   stay **unchanged** — that test is not edited. Editing those numbers would be an oracle-integrity
   failure, not a fix.

   **Ranking shape.** The helper's `ROW_NUMBER()` partitions by
   `(river_network_version_id, h.cycle_time)` and its SELECT list gains `h.cycle_time`, so one query
   answers both questions: `national_discharge_cycles` needs every cycle per network, and the no-arg
   `national_discharge_valid_times` path picks the max-`cycle_time` row per network **in Python**.
   That is a real change to the no-arg branch's SQL text and result columns, so the existing fake-row
   oracle must gain a `cycle_time` column (see decision 13) — the change is recorded, not hidden.
   The four text assertions `tests/test_hydro_display_mvt_scaling.py:124-127` makes
   (`mi.basin_version_id = h.basin_version_id` present, `hydro.run_display_coverage` present,
   `mi.model_id = h.model_id` absent, `hydro.river_timeseries` absent) MUST still hold verbatim.
   Note what is NOT an oracle here: the `ROW_NUMBER() OVER` / `ORDER BY h.cycle_time DESC, h.run_id DESC`
   / `AND mi.active_flag` string assertions at `:80-82` belong to
   `test_national_source_generations_change_with_data_identity`, which drives
   `national_discharge_source_version` — a **different** statement this PR does not touch. The ranking
   change therefore has no inherited string pin, and its only oracle is the new behavioral case in
   mutation 29a. Write that case; do not claim the existing strings cover it.

   **Order of operations (load-bearing).** The existing rectangle validation fails the *whole* discovery
   closed when *any* returned row is malformed (`services/tiles/mvt.py`, the loop that returns an empty
   `ValidTimeDiscovery` on a bad row). With one row per `(network, cycle)` instead of one per network,
   validating first would let a single malformed historical cycle blank out the no-arg result — and thus
   the catalog's `metadata.valid_times` — which master never does. Therefore: **select the rows in scope
   first (no-arg → the max-`cycle_time` row per network; `(source, cycle)` → the rows for that cycle),
   then validate only those.**
2. **Placement is load-bearing.** Five test surfaces slice `services/tiles/mvt.py` by function-name
   markers:
   - `def valid_times_for_layer` → `def national_discharge_valid_times`: `tests/test_migrations.py:392`,
     `tests/test_river_ts_read_path_surrogate_keys.py:230` and `:561`, `tests/river_ts_template_registry.py:368`.
   - `def national_discharge_valid_times` → `def _valid_time_discovery`:
     `tests/test_display_publish_status_only.py:168`.
   - `def valid_times_for_layer` → `def _valid_time_discovery`:
     `tests/test_sql_shape_helpers.py:695-698`
     (`test_python_source_surfaces_reduce_to_real_sql_before_their_pins_run`), which runs the whole slice
     through `sql_from_python` + `strip_scalar_subqueries` and asserts `FROM hydro.river_timeseries`
     present, `SELECT run_key FROM hydro.hydro_run` and `enum_range` absent. The new helper's SQL lands
     inside this slice.

   Consequently: `national_discharge_valid_times` MUST remain the first `def` after
   `valid_times_for_layer` (nothing new inserted between them), and `_national_discharge_coverage_rows`
   + `national_discharge_cycles` MUST be defined **after** `def national_discharge_valid_times` and
   **before** `def _valid_time_discovery`. Four of the five files MUST be in the verification command
   (decision 14), otherwise this constraint has no oracle in the PR lane; the fifth,
   `tests/river_ts_template_registry.py`, is not a `test_*.py` module and is not collected — it runs
   transitively through `tests/test_sql_shape_helpers.py`'s `REGISTRY` parametrization, which is in the
   command.
3. **`cycles[].valid_time_start` / `.valid_time_end` are the clamped stride endpoints, not the raw
   coverage bounds.** `run_display_coverage` is an **hourly** grid (the existing rectangle check pins
   `end - start == (lead_count - 1) * 3600`), so a coverage bound need not fall on the 3-hour stride.
   Decided: each listed cycle's `valid_time_start` / `valid_time_end` are the **first and last entries of
   that cycle's clamped 3-hour list** — the same values `valid-times?source=&cycle=` returns — so the two
   endpoints cannot disagree. A cycle whose clamped window contains no stride instant is **not listed**
   (fail-closed, same as an uncovered network); `cycles[]` is sorted **descending** by `cycle_time`.
4. **URL templates (not pinned by any spec).** Decided:
   - `cycles_url_template = "/api/v1/layers/discharge/cycles?source={source}"`
   - `valid_times_url_template = "/api/v1/layers/discharge/valid-times?source={source}&cycle={cycle}"`
   Placeholder syntax is the `{name}` form already used by `tile_url_template`, so one frontend
   substitution routine serves all three. `required_placeholders` describes the **tile** template only
   and is unchanged in meaning: `["source", "cycle", "valid_time", "z", "x", "y"]`.
5. **`national_discharge_valid_times` keeps returning `ValidTimeDiscovery`.** Issue #2009's
   "Key interfaces" writes `-> list[str]`; every caller uses `.valid_times` / `.limit` /
   `.observed_count` / `.truncated` / `.model_dump()`, and `LayerValidTimesResponse` serializes
   `model_dump()`. The issue line is shorthand for the payload, not a signature change. Recorded as a
   deviation from the issue text.

   **Truncation semantics of the per-cycle branch** (undefined by the spec, and NOT inheritable from the
   no-arg branch, which keeps the **last** `limit` entries — `services/tiles/mvt.py`'s
   `retained_start = common_end - ...` — and would therefore contradict "first entry == cycle"):
   `observed_count` = the untruncated count of 3-hour-stride entries in the clamped window;
   `limit` = `MVT_VALID_TIME_SAMPLE_LIMIT`; `truncated` = `observed_count > limit`; and truncation keeps
   the **first** `limit` entries. A 168 h horizon yields 57 entries and never truncates today; the rule
   is pinned anyway so a longer horizon does not get its behavior decided by the reused old branch.
6. **Argument-shape rejections (fail-closed, all 422 `VALIDATION_ERROR`).** The spec pins only
   "unknown source" and "cycle without source". Decided additionally:
   - `source` given without `cycle` on `valid-times` → 422. A source alone has no defined window; the
     alternative (silently ignoring `source`) would serve gfs times under an ifs request.
   - `run_id` combined with `source`/`cycle` on `valid-times` → 422. The two selectors name different
     identities; honouring both is undefined.
   - `source`/`cycle` on `valid-times` for any `layer_id != "discharge"` → 422. Only `discharge` has a
     national source/cycle contract.
   - a `cycle` that parses but is not seconds-precision-representable (non-zero microseconds) → 422,
     inheriting I4 decision 2 verbatim. `...T12:00:00.000Z` and `...T12:00:00+00:00` are accepted and
     canonicalize onto `...T12:00:00Z`.
7. **`valid-times?source=&cycle=` is intersection-scoped, like `cycles`.** When the requested
   `(source, cycle)` lacks a display-ready run in **any** active network, the response is the empty
   discovery (`valid_times = []`), not a partial list over the networks that do have one. Same
   fail-closed rule as `cycles`; without it the endpoint would hand the frontend times for a cycle the
   catalog refuses to list.
8. **The advertised list is clamped to the intersection window — an explicit supersession.** The list is
   generated from `cycle` at 3-hour stride, but restricted to
   `[max(cycle, max(river_valid_time_start)), min(river_valid_time_end)]` across the networks for that
   identity. In the production case (coverage starts at the cycle) this is exactly the 57 entries
   `C … C+168h` the acceptance criteria name; when coverage starts late, the clamp is what stops the API
   advertising an instant no basin can render — a direct consequence of the governing invariant above.

   This **supersedes** the earlier wording of `specs/mvt-tile-contract/spec.md` ("return valid times from
   `cycle` at 3-hour stride up to the minimum `river_valid_time_end`") and narrows issue #2009's
   acceptance-criterion phrase "首项 = C" to the fully-covered case. Following the I4 precedent, the
   supersession is not left in this addendum alone: the spec sentence has been amended in the same commit
   and two scenarios added (`Coverage that starts after the cycle clamps the first entry`,
   `A cycle outside the intersection has no valid times`), and the narrowing is recorded in the PR's
   `偏离记录`. Not clamping is the alternative, rejected: it re-opens the partial-render failure the
   fail-closed decision exists to close.
9. **`metadata.version` hash input carries the four new fields, and only for the national discharge
   entry.** `default_source`, `default_cycle`, `cycles_url_template`, `valid_times_url_template` are
   added to `_stable_json_hash`'s input dict **only when `national_discharge` is true**, so the
   `river-network` / `met-stations` / single-run entries keep their current `metadata.version` byte for
   byte. The discharge entry's version deliberately rotates once (its contract changed). Runless vs
   `?run_id=<X>` byte-identity is preserved because none of the four depend on `run_id`.
10. **The catalog calls the same function the spec names.** `overview-data-contracts` states that
   `metadata.valid_times` MUST be sourced from
   `national_discharge_valid_times(session, source=default_source, cycle=default_cycle)`, and
   `frontend-mvt-layer-consumption` forbids the frontend from re-fetching the list when the active
   identity equals the defaults — i.e. the catalog list and the endpoint list MUST be byte-identical.
   `_default_layer_catalog` therefore calls `national_discharge_cycles(session, source="gfs")` for
   `default_cycle` and then `national_discharge_valid_times(session, source=..., cycle=...)` for the
   list — two national discovery queries in the discharge branch, not one, and no private stride path
   that could drift from the endpoint. (An earlier draft of this addendum derived the list from the
   cycles rows to save a round trip; rejected, because the equivalence the two specs require would then
   rest on a duplicated stride implementation with no oracle.) Both share
   `_national_discharge_coverage_rows`, so the stride logic exists once. The cold p95 budget is
   measured, not assumed: the node-27 receipt records `GET /api/v1/layers` before and after, and a
   regression against the budget is a finding, not an accepted cost. **The threshold is ≤ 400 ms, not
   the ≤ 200 ms this addendum originally carried** — a user decision taken at i5-2009 review round 1
   after two node-27 receipts measured master's own pre-change cold p95 at 331–392 ms, i.e. the 200 ms
   figure was unmet before this change existed. `overview-data-contracts`'s "Cold `/api/v1/layers`
   budget" scenario carries the same 400 ms and the same reasoning; Epic #2003's acceptance item 2 still
   spells 200 ms and is amended outside this PR (recorded in the PR's 偏离记录).
   The receipt is not just two latency numbers. It MUST also record, for the same run:
   (a) `SELECT count(DISTINCT river_network_version_id) FROM core.model_instance WHERE active_flag` —
   the intersection denominator; (b) the coverage query's returned row count and the observed
   `covered_networks` for the winning cycle, asserted equal to (a); (c) `len(cycles)` and
   `default_cycle` from the runless catalog, asserted non-empty / non-null for `gfs`. Without (a)-(c) a
   green latency number is indistinguishable from a fail-closed empty intersection that is fast because
   it returns nothing.
   `hydro_display.py:262`'s `national_discharge_source_version(session)` stays **argument-free** —
   pinned by `test_layer_catalog_still_digests_every_source_not_one_identity` (I4 mutation row 20).
11. **Cache keys.** `cycles` → `discharge-cycles:{source}`. `valid-times` →
    `valid-times:{layer_id}:{run_id}:{source}:{canonical_cycle}` where `canonical_cycle` is the
    canonicalized spelling, so `...T12:00:00.000Z` and `...T12:00:00Z` share one entry. `/api/v1/layers`
    keeps `layers:{run_id}:{limit}:{offset}` (explicitly permitted by `overview-data-contracts`; the
    discharge entry's content is run-agnostic regardless).
12. **Zero display-ready runs anywhere still yields `data: []`.** The empty-catalog gate is
    `display_ready_run(session) is None` in `list_layers`, upstream of the discharge entry. The
    empty-intersection case (runs exist, no common cycle) is the *other* branch and MUST still return the
    entry. These are two different states and both have their own regression row.
13. **Existing oracles that must be updated, and how.** Only these, and only in the direction that keeps
    their behavioral assertions intact:
    - `tests/test_api_contract.py:1385-1402` — monkeypatches `national_discharge_valid_times =
      lambda _session: _ValidTimes()` against an `object()` session. Decision 10 makes
      `_default_layer_catalog` also call `national_discharge_cycles`, which that patch does not cover
      (`object().execute` → `AttributeError`). Both symbols must be patched, and the call signatures
      must accept the new keyword arguments. tasks.md 3.6 calls `:1409` the only assertion in this file
      needing an update; the monkeypatch at `:1388` is a second one, recorded here.
    - `tests/test_hydro_display_mvt_scaling.py:88-147` — the two no-arg fake-row oracles gain a
      `cycle_time` column (decision 1's ranking change) and one of them gains a second cycle for the same
      network so the "no-arg picks the newest cycle per network" claim has an oracle. Their asserted
      `valid_times` / `observed_count` values and their SQL-text assertions do NOT change.
    - `tests/test_hydro_display_mvt_scaling.py:191` (inside
      `test_national_river_metadata_is_versioned_pbf`) is the `river-network-national` template assertion
      and is NOT touched by this change (tasks.md 3.6 is authoritative over the issue body's contrary
      line; its `:175` is a stale line number that now lands in a different test). The issue body's and
      tasks.md's `services/tiles/mvt.py:1340` reference for the `_layer_source_refs` entry assertion is
      likewise stale — it is at `:1500`.
    - **No other test is edited.** In particular `tests/test_river_ts_read_path_surrogate_keys.py`
      (`:848-868`) pins the `#1342` marker/aid census over `services/tiles/mvt.py` at 18/24, and its
      registry closure (`:871-889`) balances on occurrences of `hydro.river_timeseries` in string
      literals. The new helper queries `hydro_run` / `model_instance` / `run_display_coverage` only, adds
      no compression-pushdown aid and no `river_timeseries` literal, so those counts MUST stay unchanged
      — a diff that moves them means the implementation reached into the timeseries read path and is a
      scope escape, not a census to update.
14. **Verification command.** The Evidence Floor command for this issue is:
    `uv run pytest tests/test_hydro_display_mvt_scaling.py tests/test_api_contract.py
    tests/test_openapi_drift.py tests/test_openapi_31_contract.py tests/test_display_publish_status_only.py
    tests/test_sql_shape_helpers.py tests/test_migrations.py tests/test_river_ts_read_path_surrogate_keys.py -q`
    — the last three are what give decisions 1 and 2 an oracle in the PR lane; without them a placement
    or SQL-shape violation is only found by the post-merge master run. Plus `uv run ruff check .`,
    `cd apps/frontend && pnpm check:api-types`, and (because `src/api/types.ts` changes and CI's path
    scope will run the frontend job) `pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm test`.

15. **Cycle-dimension bound on `national_discharge_cycles` (user decision, review round 1).** The
    coverage query behind `cycles` is unbounded in the cycle dimension: every cycle a network ever had a
    display-ready run for stays in the scan and, once fully covered, in the response, so both the query
    cost and `len(cycles)` grow linearly with the pipeline's lifetime. Review round 1 raised this as
    cand-02 and the verifier ruled that landing a bound *silently* would be an oracle violation, because
    it narrows the normative "every active river network" sentence and changes a fixture-named call
    shape. The user's call was to amend the fixture and add the bound in this PR. Its shape:
    - `national_discharge_cycles` passes `since = now(UTC) - timedelta(days=NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS)`;
      the constant is module-level in `services/tiles/mvt.py` and set to **14**, matching
      `scripts/node27_raw_retention.py:33`'s `DEFAULT_RETENTION_DAYS` and the `NHMS_RETENTION_DAYS` /
      `NODE27_RAW_RETENTION_DAYS` defaults. `mvt.py` MUST NOT read the environment for it — the display
      read path has no env surface today and adding one is a different change.
    - The predicate lives in `_national_discharge_coverage_rows`'s SQL as
      `AND (CAST(:since AS timestamptz) IS NULL OR h.cycle_time >= :since)` — the same NULL-guard idiom
      as the existing `:source` / `:cycle` binds (decision 6), never a `::timestamptz` cast.
    - **Every other caller passes `since=None`.** `national_discharge_valid_times` passes `None` on both
      its branches: the per-cycle branch already binds `:cycle`, and the no-arg branch is pinned
      byte-identical to master by its own regression row. This is what keeps the no-arg path unchanged —
      a network whose newest display-ready run predates the window is *included* by master, and a
      `:since` on that path would drop it and change the intersection.
    - **Second, independent reason the no-arg branch must pass `None`, found while implementing:**
      `NULL >= :since` evaluates to NULL, so a bound `:since` also silently drops every run with a NULL
      `cycle_time` — exactly the rows decision 1 / FIX-3 keeps *first* in the no-arg ranking to match the
      two untouched tile CTEs (PostgreSQL `ORDER BY ... DESC` implies NULLS FIRST). Binding `:since` on
      that branch would therefore break the tile-CTE agreement as well as the intersection, and the
      `_NationalDiscoverySession` filter must reproduce the same NULL semantics (drop, not keep) or the
      fake diverges from the SQL it stands in for.
    - **The test fixture must not rot by calendar.** Because the bound is `now()`-relative while ~10
      existing cases pin absolute `2026-09-02`-era cycle literals, those cases would all start failing on
      a wall-clock date once the fake filters on `since` — a defect that appears with no code change. A
      module-level autouse fixture widens `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS` for the file and the
      three cases that are actually about the bound monkeypatch the real 14 back. Rewriting every legacy
      case to relative instants is the alternative and touches far more of the decision-13 protected
      files; it was rejected for that reason. `test_national_cycle_lookback_is_the_raw_retention_default`
      pins the literal 14 so the widening cannot hide a changed constant.
    - Consequence, stated so round 2 does not rediscover it: the bound anchors on `now()`, not on the
      retention watermark, so an ingest stall longer than 14 days empties `cycles[]` →
      `default_cycle = null` → the national layer renders disabled even though renderable tiles still
      exist. That is the same fail-closed state as an empty intersection (decision 3) and is accepted.
    - Consequence, second: this bounds `cycles[]` and the catalog's discharge branch only. The runless
      `/valid-times` route's own row growth is **not** bounded here and remains a known limit routed to
      I13, whose prewarm rewrite is its only consumer.
    - The index that serves the range scan is `hydro_run_latest_ready_run_idx` (`db/migrations/000021`,
      `(cycle_time DESC, run_id DESC)`), because `cycle_time` is its leading column. The
      `hydro_run_display_ready_*` family (`000024` / `000030` / `000040`) leads with
      `(LOWER(source_id), run_type, basin_version_id, …)` and the coverage query binds no `run_type`, so
      `cycle_time` is not a usable leading prefix there. No new migration is needed.
    - Oracle: `tests/test_hydro_display_mvt_scaling.py` gains a case with one fully-covered cycle inside
      the window and one older, asserting only the newer is listed and is `default_cycle`. For that test
      to be red-capable, `_NationalDiscoverySession.execute` (`tests/test_hydro_display_mvt_scaling.py:1305-1331`) must filter on the `since`
      bind the way it already filters on `source` and `cycle`; without that the predicate could be
      deleted and nothing would fail. The four SQL-text assertions at `:159-162` are substring `in`
      checks and are unaffected. Editing `_NationalDiscoverySession` and adding this case is the one
      exception to decision 13's "no other test is edited" — the file is already in decision 13's
      allowlist, the census in `tests/test_river_ts_read_path_surrogate_keys.py` is not touched, and the
      `h.status IN (...)` occurrence counts pinned at 2/1/1/5 by
      `tests/test_display_publish_status_only.py` MUST stay unchanged (the new predicate is on
      `h.cycle_time`, not on status, and adds no second SQL shape).

## Surfaces

- **Producers**: `services/tiles/mvt.py::_national_discharge_coverage_rows` (new, sole owner of the
  national display-ready predicate), `::national_discharge_cycles` (new), `::national_discharge_valid_times`
  (gains `source`/`cycle`), `::layer_metadata` (four new discharge-only fields + hash input),
  `::_NATIONAL_DISCHARGE_METADATA` (new tile template + six-tuple placeholders).
- **Validators/preflight**: `apps/api/routes/hydro_display.py` — the `gfs|ifs` enum and RFC3339/seconds
  canonicalization already added for the tile route by #2007 (reused, not re-implemented), plus the new
  argument-shape rejections of decision 6.
- **Storage/cache/query**: `apps/api/display_cache.py::display_catalog_cached` keys for `layers`,
  `valid-times`, `cycles`.
- **Public routes/entrypoints**: `GET /api/v1/layers` (BREAKING discharge entry),
  `GET /api/v1/layers/{layer_id}/valid-times` (new optional query params),
  `GET /api/v1/layers/discharge/cycles` (new).
- **Frontend/downstream consumers**: `apps/frontend/src/api/types.ts` (regenerated only), and OUT of this
  PR: `M11Shell.test.tsx` fixture, `buildM11RegisteredOverlay`, the control bar (I10/I12);
  `scripts/node27_mvt_prewarm.py` (I13). This PR MUST NOT edit them.
- **Failure paths/rollback/stale state**: empty intersection (entry returned, `default_cycle = null`),
  zero display-ready runs (`data: []`), unknown/not-ready `run_id` (404 / not-ready envelope, whole
  catalog blocked), argument-shape 422s.
- **Evidence/audit/readiness**: `apps/api/openapi_patching.py::_patch_layer_metadata_openapi` +
  `_layer_metadata_schema` (the four new fields and the cycles response schema must reach the runtime
  schema), `openapi/nhms.v1.yaml` (hand-written, equality-compared),
  `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema`,
  `tests/test_display_publish_status_only.py` (predicate counts, unchanged), node-27 live receipt
  (`cycles`/`valid-times` bodies + `GET /api/v1/layers` cold p95 before/after).

## Regression rows

| Surface + input | Expected behavior |
|---|---|
| `national_discharge_cycles(session, "gfs")`, 38 networks have cycle A, 37 have cycle B | `cycles` contains A, not B; `default_cycle == A` |
| same, three intersected cycles | `cycles` is sorted strictly descending by `cycle_time`; `default_cycle == cycles[0].cycle_time` |
| same, a cycle whose clamped window holds no 3-hour stride instant | that cycle is not listed (decision 3) |
| same, one active network has zero gfs display-ready runs | `cycles == []`, `default_cycle is None` |
| same, a network's run exists but `segment_count == 0` | that network counts as uncovered → cycle excluded |
| same, two fully-covered cycles: one inside the 14-day lookback window, one older | only the newer is listed; `default_cycle` is the newer (decision 15) |
| same, every fully-covered cycle is older than the lookback window | `cycles == []`, `default_cycle is None` — the ingest-stall case, fail-closed by design (decision 15) |
| `national_discharge_valid_times(session, source="gfs", cycle=C)`, all networks cover `C+168h` | 57 entries, first `== C`, last `== C+168h`, adjacent delta 3 h |
| same, one network ends at `C+96h` (non-rectangular) | list truncated at `C+96h` |
| same, `(source, cycle)` missing in one active network | `valid_times == []` (decision 7) |
| `national_discharge_valid_times(session)` (no args) | byte-identical result to pre-change master for the same rows |
| `national_discharge_valid_times(session)` (no args), one network with two cycles | picks the newest cycle's run for that network (the pre-change `rn = 1` semantics, now decided in Python) |
| `national_discharge_valid_times(session)` (no args), one network has an older cycle with malformed coverage plus a well-formed newest cycle | returns the newest cycle's window — the older row is discarded by selection **before** validation (decision 1, order of operations); it does NOT blank the whole discovery |
| `national_discharge_valid_times(session, source="gfs", cycle=C)`, one network starts at `C+6h` | first entry is `C+6h`, not `C` (decision 8) |
| cross-endpoint consistency: for every listed cycle `K`, `cycles[K].valid_time_start` / `.valid_time_end` | equal the first / last entry of `valid_times(source, K)` |
| catalog vs endpoint: `discharge` `metadata.valid_times` | equals `national_discharge_valid_times(session, source=default_source, cycle=default_cycle).valid_times` on the same session |
| per-cycle list longer than `MVT_VALID_TIME_SAMPLE_LIMIT` | first `limit` entries kept, `truncated is True`, `observed_count` = untruncated count (decision 5) |
| `GET /api/v1/layers/discharge/cycles?source=ERA5` / missing `source` | 422, no SQL executed |
| `GET .../valid-times?cycle=C` (no source), `?source=gfs` (no cycle), `?source=gfs&cycle=C&run_id=R`, `?source=gfs&cycle=C` on `river-network` | 422 each, no SQL executed |
| every instant in either response body | matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` |
| `GET /api/v1/layers` runless **and** `?run_id=<X>` | discharge entry byte-identical: template, six-tuple placeholders, `default_source == "gfs"`, same `default_cycle`, same `valid_times`, `source_refs == {}`, same `metadata.version`; `maplibre_source_layer == "hydro"`; `properties` contains `basin_id` |
| `GET /api/v1/layers`, runs exist but intersection empty | discharge entry returned with `default_cycle is None`, `valid_times == []` |
| `GET /api/v1/layers`, zero display-ready runs | `data == []` (no ghost discharge entry) |
| `GET /api/v1/layers?run_id=<unknown>` / `<not-display-ready>` | 404 `RUN_NOT_FOUND` / not-ready envelope; no discharge side-channel |
| unchanged sibling: `river-network` entry, **runless** (`national=True` for river-network today) | `tile_url_template == "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf"`, `required_placeholders == ["z","x","y"]`, `metadata.version` unchanged from master |
| unchanged sibling: `river-network` entry, **run-scoped** (`?run_id=<X>`) | `tile_url_template == "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf"`, `required_placeholders == ["basin_version_id","z","x","y"]`, `metadata.version` unchanged from master |
| unchanged sibling: `postgis_tile_sql("hydro-national")` and the legacy 5-segment tile route | untouched by this PR (I4 surface) |
| `_layer_source_refs(layer_id="discharge", ...)` | raises `AssertionError` |
| runtime `app.openapi()` | equals `openapi/nhms.v1.yaml`; contains the cycles path and the `source`/`cycle` query params; `LayerMetadata` schema carries the four new fields |
| `tests/test_display_publish_status_only.py` | green **unchanged** (tile_sql 2 / digest 1 / valid-times slice 1 / module 5) |
| `tests/test_sql_shape_helpers.py::test_python_source_surfaces_reduce_to_real_sql_before_their_pins_run` | green unchanged with the new helper SQL inside the slice |

## Mutation matrix

Inherited discipline from I4: **every behavioral claim must have a red-capable oracle. A predicate
present in the SQL string but ineffective, or an argument passed but unasserted, must make at least one
test fail. String-shape assertions are not oracles.** The implementer MUST run each mutation, record the
measured pass/fail counts, and extend the table rather than re-derive it.

`local` = the pytest command in decision 14 (eight files, including the three placement/SQL-shape
oracles). `Measured` is filled in by the implementer from the actual run — `local N pass / M fail` —
and is what makes the row evidence rather than an intention. Rows 1–30 (plus 4a/7a/29a/29b/29c) are the
original 35; rows 31–34 were added at review round 1 for the four fixes FIX-1..FIX-4 and rows 36/36b/37
for the cycle-dimension bound (decision 15, FIX-6) — **35 is deliberately unused**, so the table has 42
rows with one numbering gap. FIX-6 changed the oracle mechanism itself (`_NationalDiscoverySession` now
also filters on the `since` bind, and four tests were added), which invalidates any count taken before
it; every `Measured` cell in this table is therefore measured against the post-FIX-6 baseline of
`local 360 passed`.

|#|Claim|Mutation|Test that must go red|Measured|
|---|---|---|---|---|
|1|The intersection is an intersection, not a union|drop the `covered_networks == active_network_total` condition in `national_discharge_cycles`|partial-coverage case (cycle B) starts appearing in `cycles`|`local 358 pass / 2 fail`|
|2|A network with **zero** runs for the source fails the whole list closed|compute the network total from the rows returned instead of from `core.model_instance`|the "one network has no gfs run at all" case returns a non-empty `cycles`|`local 356 pass / 4 fail`|
|3|`segment_count > 0` is load-bearing|delete that predicate|**node-27 lane only — no local oracle.** SQL semantics: a zero-segment run then wins `rn = 1` for its `(network, cycle)`, so a **covered** cycle disappears from `cycles` (the predicate sits in the JOIN `ON` upstream of `ROW_NUMBER()`, `services/tiles/mvt.py:1997`) — it is liveness, not safety. But `_NationalDiscoverySession` (`tests/test_hydro_display_mvt_scaling.py:1292-1334`) never executes SQL; it matches on `"hydro.run_display_coverage" in sql` and returns its own canned rows, so a predicate inside the JOIN `ON` is unobservable locally for *any* row data. The only local signal would be a string-shape assertion, which this preamble does not accept as an oracle. Recorded honestly rather than padded|`local 360 pass / 0 fail` — no local oracle, see the claim column|
|4|`default_cycle` is the **newest** intersected cycle|reverse the sort / take `cycles[-1]`|default-cycle assertion|`local 358 pass / 2 fail`|
|4a|`cycles[]` itself is descending|drop the sort (return DB/dict order)|the three-intersected-cycles ordering case|`local 359 pass / 1 fail`|
|5|The stride is 3 h, not 1 h|`timedelta(hours=3)` → `hours=1`|57-entry / adjacent-delta case|`local 351 pass / 9 fail`|
|6|The list is clamped to `min(river_valid_time_end)`|drop the upper clamp|non-rectangular-coverage case|`local 357 pass / 3 fail`|
|7|…and to the coverage start (decision 8)|drop the lower clamp|the `one network starts at C+6h` regression case|`local 356 pass / 4 fail`|
|7a|`cycles[].valid_time_start` uses the same clamp as the list|clamp the list but leave `cycles[].valid_time_start = cycle_time`|the cross-endpoint consistency case|`local 359 pass / 1 fail`|
|8|`source` reaches the SQL bind|drop `source=` from the helper call in `national_discharge_cycles`|an ifs-only cycle appears in the gfs list|`local 359 pass / 1 fail`|
|9|`cycle` reaches the SQL bind|drop `cycle=` from the helper call in `national_discharge_valid_times`|the per-cycle list stops depending on the requested cycle|`local 357 pass / 3 fail`|
|10|The no-arg path still exists and is exercised|make `source`/`cycle` required kwargs on `national_discharge_valid_times`|the two no-arg cases in `tests/test_hydro_display_mvt_scaling.py` and the no-argument `valid-times` route case (`TypeError`). NOT the catalog — decision 10 makes it pass both kwargs|`local 353 pass / 7 fail`|
|11|The catalog's four new metadata fields exist and are correct|delete any one of `default_source` / `default_cycle` / `cycles_url_template` / `valid_times_url_template`|catalog shape assertion|`local 351 pass / 9 fail`|
|12|…and they reach the `metadata.version` hash input|remove them from `_stable_json_hash`'s dict|a test that pins the discharge `metadata.version` against a recomputed hash including them|`local 357 pass / 3 fail`|
|13|…and they do NOT reach non-discharge entries' hash input|add them unconditionally|`river-network` `metadata.version` moves off its pinned value|`local 359 pass / 1 fail`|
|14|Runless and run-scoped discharge entries are byte-identical|make `default_cycle` depend on the requested `run_id`|two-call identity assertion|`local 355 pass / 5 fail`|
|15|The empty intersection still returns the entry|return `None`/skip the entry when `default_cycle is None`|empty-intersection catalog case|`local 358 pass / 2 fail`|
|16|…and is distinct from the zero-run empty catalog|synthesize a discharge entry when `display_ready_run` is `None`|zero-run `data == []` case|`local 359 pass / 1 fail`|
|17|`cycle` without `source` is rejected before SQL|delete the guard|422 case (session whose `execute` raises)|`local 359 pass / 1 fail`|
|18|`source` without `cycle` is rejected|delete the guard|422 case|`local 359 pass / 1 fail`|
|19|`run_id` + `source`/`cycle` is rejected|delete the guard|422 case|`local 359 pass / 1 fail`|
|20|`source`/`cycle` on a non-discharge layer is rejected|delete the guard|422 case|`local 359 pass / 1 fail`|
|21|The source enum rejects `ERA5`/`best`/`GFS`-cased input before SQL|widen the enum check|422 case|`local 354 pass / 6 fail`|
|22|Spellings collapse onto one cache entry|drop the canonicalization from the cache key|`test_valid_times_cache_key_collapses_spellings_and_separates_identities`. Note the discriminating spelling is the offset form (`...T20:00:00+08:00`), not the `...T12:00:00.000Z` / `...T12:00:00Z` pair this row originally named — FastAPI's datetime parsing already folds those two onto the same `datetime` before the key is built, so they cannot separate entries. The test is correct; the row's wording was not|`local 359 pass / 1 fail`|
|23|`(source, cycle)` separates two cache entries|drop `source` or `cycle` from the key|two-identity cache case|`local 359 pass / 1 fail`|
|24|Every instant is seconds-precision|emit `isoformat()` instead of `canonical_mvt_time`|regex assertion over every field|`local 341 pass / 19 fail`|
|25|The cycles route is in the runtime schema and the yaml|delete either the yaml block or the route|`test_static_openapi_matches_runtime_schema`|`local 352 pass / 8 fail`|
|26|`LayerMetadata` documents the four new fields|delete them from `_layer_metadata_schema`|drift test (yaml/runtime divergence)|`local 357 pass / 3 fail`|
|27|`_layer_source_refs` refuses `discharge`|delete the `assert`|the new `AssertionError` case|`local 359 pass / 1 fail`|
|28|The status predicate count is untouched|add a second `h.status IN (...)` to the new helper|`tests/test_display_publish_status_only.py:192` (valid-times slice 1→2) and `:193` (module 5→6); `:187`/`:191` do NOT move|`local 359 pass / 1 fail`|
|29|The catalog's list is the endpoint's list|change the stride or the lower bound in one caller only (e.g. inline a second stride computation in `_default_layer_catalog`)|the catalog-vs-endpoint equality case|`local 355 pass / 5 fail`|
|29a|The no-arg branch still ranks latest-cycle-per-network|drop the max-`cycle_time` selection in the no-arg Python path|the `one network with two cycles` case|`local 359 pass / 1 fail`|
|29c|Selection happens before validation|validate all returned rows first, then select|the `older malformed cycle` case (whole discovery blanks)|`local 359 pass / 1 fail`|
|29b|Truncation keeps the FIRST entries in the per-cycle branch|reuse the no-arg `retained_start = end - ...` tail-keeping logic|the over-limit truncation case|`local 359 pass / 1 fail`|
|30|`hydro_display.py:262`'s digest call stays identity-free|pass `source="gfs"`|`test_layer_catalog_still_digests_every_source_not_one_identity` (I4)|`local 359 pass / 1 fail`|
|31|The per-cycle branch fails closed on an off-phase hourly coverage grid — the per-cycle twin of the no-arg branch's `(common_end - start) % 3600` guard (review round 1, FIX-1: this change had dropped the guarantee on the new path)|delete the `int((window[0] - cycle).total_seconds()) % 3600` guard in `_national_cycle_valid_times`|`test_national_per_cycle_valid_times_fail_closed_for_an_off_phase_coverage_grid`|`local 359 pass / 1 fail`|
|32|The catalog never advertises `default_cycle = C` together with `valid_times = []` (review round 1, FIX-2: READ COMMITTED gives each of the two calls its own snapshot)|delete the `if not valid_time_sample.valid_times: default_cycle = None` forcing in `_default_layer_catalog`|`test_layer_catalog_never_advertises_a_cycle_whose_timeline_came_back_empty`|`local 359 pass / 1 fail`|
|33|A NULL `cycle_time` ranks FIRST in the no-arg branch, reproducing PostgreSQL's `ORDER BY ... DESC` = NULLS FIRST and the two untouched national tile CTEs (review round 1, FIX-3)|`datetime.max` sentinel → `datetime.min` in `_national_run_rank`|`test_no_argument_national_valid_times_rank_a_null_cycle_first_like_the_tile_ctes`|`local 359 pass / 1 fail`|
|34|The cycles cache key separates the two sources (review round 1, FIX-4: decision 11 pinned the spelling with no oracle behind it)|drop `{source}` from `f"discharge-cycles:{source}"`|`test_cycles_cache_key_separates_the_two_sources`|`local 359 pass / 1 fail`|
|36|The lookback predicate actually bounds the **DB scan**|drop `AND (CAST(:since AS timestamptz) IS NULL OR h.cycle_time >= :since)` from the SQL, keep the bind|**node-27 lane, same as row 3.** No local oracle: `_NationalDiscoverySession` filters by bound values and deliberately never by SQL text (`tests/test_hydro_display_mvt_scaling.py`, the `_NationalDiscoverySession` docstring), so with the bind still passed the fake keeps filtering and nothing about the scan is observable locally. The local signal is a predicate-text assertion in `test_national_cycles_list_only_cycles_inside_the_lookback_window`, recorded below but **not counted as an oracle** — the preamble disowns string-shape assertions. Row 36b carries the behavioral half; the node-27 receipt (row-count + `EXPLAIN (ANALYZE, BUFFERS)` showing `hydro_run_latest_ready_run_idx`) carries the scan half|`local 359 pass / 1 fail` (text pin only)|
|36b|…and it is actually applied — the bound value reaches the query|`national_discharge_cycles` passes `since=None`|`test_national_cycles_list_only_cycles_inside_the_lookback_window` + `test_national_cycles_are_empty_when_every_covered_cycle_predates_the_lookback` — the older cycle is listed again and the all-stale case stops being empty|`local 358 pass / 2 fail`|
|37|…and only `cycles` passes it — the no-arg path stays unbounded on purpose|pass the same `since` from `national_discharge_valid_times`'s no-arg branch|two tests, for two independent reasons: `test_no_argument_national_valid_times_keep_a_network_whose_newest_run_predates_the_lookback` (the intersection-membership reason) and `test_no_argument_national_valid_times_rank_a_null_cycle_first_like_the_tile_ctes` (the NULL-cycle reason below). Note the byte-identical no-arg regression case this row originally named does **not** go red — every network there has a recent newest run — so the first of the two tests had to be written for this row|`local 358 pass / 2 fail`|
