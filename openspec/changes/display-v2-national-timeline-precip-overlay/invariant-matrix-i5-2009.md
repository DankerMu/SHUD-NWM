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
   The four text assertions `tests/test_hydro_display_mvt_scaling.py:161-164` makes
   (`mi.basin_version_id = h.basin_version_id` present, `hydro.run_display_coverage` present,
   `mi.model_id = h.model_id` absent, `hydro.river_timeseries` absent) MUST still hold verbatim.
   Note what is NOT an oracle here: the `ROW_NUMBER() OVER` / `ORDER BY h.cycle_time DESC, h.run_id DESC`
   / `AND mi.active_flag` string assertions at `:84` belong to
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
   catalog refuses to list — with one boundary exception introduced by decision 15: `valid-times` is
   deliberately **not** given the cycle-lookback bound (both branches pass `since=None`), so for a cycle
   that has aged past the lookback window the endpoint still answers while `cycles` no longer lists it.
   That is the intended asymmetry — a bookmarked or in-flight cycle keeps working — not a leak of the
   fail-closed rule, which is about *coverage*, not about age. Bounding `valid-times` on the age
   dimension is I13's call, not this issue's (r2-int-5).
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
   regression against the budget is a finding, not an accepted cost. **The criterion is a 500 ms absolute
   ceiling plus a regression clause, not the ≤ 200 ms this addendum originally carried** — a user
   decision taken at i5-2009 review round 2, correcting the round-1 call. Round 1 set 400 ms on the
   premise that master's cold p95 is "331–392 ms"; that premise was false. The three node-27 receipts
   actually say: `issue-612-cold-waterfall-rerun-2026-06-21.md:42` three cold samples, `Median` 392 ms,
   `Max` 405 ms; `display-bootstrap-decoupling-20260620.md:100` three cold samples, `Median` 413 ms,
   `Max` 418 ms; `2026-07-20-node27-display-scaling.md:181` a single 0.331 s public sample. **No receipt
   reports a p95 for this endpoint at all** — hence the ≥ 10-sample clause. Both three-sample tables
   exceed 400 ms at their maximum, so the round-1 threshold would have failed on master. The
   corrected criterion is: **≤ 500 ms absolute** (the tier the same scenario already applies to every
   other bootstrap-critical endpoint) **AND `after − before ≤ 50 ms` inside one receipt**, each side
   from ≥ 10 cold samples taken in the same session by the same method. The ceiling alone is not
   red-capable against this change's own cost, because master's three-sample maxima already sit at
   405–418 ms; the
   regression clause is what carries that. `overview-data-contracts`'s "Cold `/api/v1/layers` budget"
   scenario carries the same two clauses and the same corrected evidence; Epic #2003's acceptance item 2
   and issue #2009's acceptance criteria still spell 200 ms and are amended outside this PR (recorded in
   the PR's 偏离记录).
   The receipt is not just two latency numbers. It MUST also record, for the same run:
   (a) `SELECT count(DISTINCT river_network_version_id) FROM core.model_instance WHERE active_flag` —
   the intersection denominator; (b) the coverage query's returned row count and the observed
   `covered_networks` for the winning cycle, asserted equal to (a); (c) `len(cycles)` and
   `default_cycle` from the runless catalog, asserted non-empty / non-null for `gfs`. Without (a)-(c) a
   green latency number is indistinguishable from a fail-closed empty intersection that is fast because
   it returns nothing.
   **Reversed in review round 2 (r2-int-1):** the catalog's discharge entry now digests the identity
   it advertises. The original text here said the catalog's call to
   `national_discharge_source_version(session)` "stays argument-free", pinned by
   `test_layer_catalog_still_digests_every_source_not_one_identity` (I4 mutation row 20). That text was
   written without noticing that the fixture had already routed the opposite here: task 3.1 (`tasks.md`)
   keeps #2007's catalog call argument-free *because the catalog change is this issue's*, and names the
   hole verbatim — "一个非最新 `(source, cycle)` 身份的 re-run 不改变 digest，`cache_key` 不变而 tile 已陈旧——该洞
   正是因为本 issue 让旧身份可寻址才被打开"; the pinning test's own docstring says the argument-free form
   holds "until I5/#2009 moves it". This PR is I5/#2009 and moves the template. The verified defect:
   the argument-free digest keeps one `rn = 1` row per network across **all** sources and cycles, so in
   the normal propagation state (some networks already hold the next cycle) it does not observe any run
   of the advertised `(default_source, default_cycle)` identity, and an `ifs` newest cycle blinds it to
   `gfs` entirely. A corrective re-run of the advertised identity then leaves `cycles[]`,
   `default_cycle`, `valid_times` and the token unchanged → `metadata.version` / `cache_version`
   unchanged → the frontend's `_mvt_cache_version` URL token and MapLibre source key are unchanged, and
   the browser keeps the superseded tiles (300 s floor via `Cache-Control: max-age=300` across reloads,
   unbounded for an open session). The server side is already correct (the canonical tile route's digest
   is identity-scoped). Remedy shape: `_default_layer_catalog` computes the discharge entry's
   `source_version` **after** `default_cycle` is resolved (and after the FIX-2 forcing), as
   `national_discharge_source_version(session, source=NATIONAL_DISCHARGE_DEFAULT_SOURCE,
   cycle=<default_cycle>)`; when `default_cycle` is `None` nothing addressable is advertised and the
   argument-free digest is used. The argument-free call moves out of `list_layers` — it is not an extra
   query, it is the same single digest query with two more binds. The **legacy alias route's** digest
   call stays argument-free (it advertises no identity). Oracle change, recorded: the pinning test is
   rewritten to assert the *opposite* (identity-scoped call for the catalog entry, argument-free for the
   alias route), and mutation row 30 inverts accordingly. Over-inclusion (an `ifs` landing rotating the
   `gfs` entry's version) goes away as a side effect.
11. **Cache keys.** `cycles` → `discharge-cycles:{source}`. `valid-times` →
    `valid-times:{layer_id}:{run_id}:{source}:{canonical_cycle}` where `canonical_cycle` is the
    canonicalized spelling, so `...T12:00:00.000Z` and `...T12:00:00Z` share one entry. `/api/v1/layers`
    keeps `layers:{run_id}:{limit}:{offset}` (explicitly permitted by `overview-data-contracts`; the
    discharge entry's content is run-agnostic regardless).
    Deferred, recorded (r2-int-6, review round 2): `/api/v1/layers` and `/api/v1/layers/discharge/cycles`
    are cached under independent keys with independent timestamps, so after an empty→non-empty
    intersection transition the catalog can still say `default_cycle = null` while `/cycles` already
    lists the cycle (worst case one TTL; typically ≤ 45 s because the warmer replays hot paths).
    No shipped consumer reads either field on this head — the surface arrives with I10/I12 — so the
    contradiction is unobservable today. Routed to I10/I12: the frontend treats `default_cycle === null`
    as authoritative over a non-empty `/cycles` list (or the two endpoints share one force-empty rule).

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

15. **Cycle-dimension bound on `national_discharge_cycles` (user decision, review round 1; value and
    rationale corrected in review round 2).** The
    coverage query behind `cycles` is unbounded in the cycle dimension: every cycle a network ever had a
    display-ready run for stays in the scan and, once fully covered, in the response, so both the query
    cost and `len(cycles)` grow linearly with the pipeline's lifetime. Review round 1 raised this as
    cand-02 and the verifier ruled that landing a bound *silently* would be an oracle violation, because
    it narrows the normative "every active river network" sentence and changes a fixture-named call
    shape. The user's call was to amend the fixture and add the bound in this PR. Its shape:
    - `national_discharge_cycles` passes `since = now(UTC) - timedelta(days=NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS)`;
      the constant is module-level in `services/tiles/mvt.py` and set to **12**. `mvt.py` MUST NOT read
      the environment for it — the display read path has no env surface today and adding one is a
      different change.
    - **Why 12, and why not 14 (round-2 correction).** The round-1 value was 14, justified as "matching
      `scripts/node27_raw_retention.py:33`'s `DEFAULT_RETENTION_DAYS`". Both halves of that were wrong.
      (a) The *form* of the coupling was wrong: "matching" asserted equality with the retention
      default, but the relation this change's own spec imposes is an inequality.
      `specs/canonical-precip-copyback/spec.md:191` requires
      `oldest_listed_cycle − 24h ≥ display_watermark − retention_days`, which with lookback `L` and
      retention `R` reduces to `L ≤ R − 1d = 13`. At `L = 14` the requirement fails in steady state and
      holds only while the pipeline is a full day behind. (b) The *reason* given was wrong too: the
      round-1 text described raw retention as deleting "raw GRIB", implying nothing on a tile path reads
      it. In this change the raw-retention run is REQUIRED to prune, on the same cutoff, the canonical precipitation
      mirror (`canonical-precip-copyback/spec.md:156-157`) and the precipitation PNG cache (`:173`) —
      which is exactly what the precipitation overlay renders from (spec-level today: task 4.4 is still unticked and `scripts/node27_raw_retention.py:8` on this HEAD still says it does not touch `canonical/`; the inequality argument does not depend on delivery), and exactly why `:191` is phrased
      against that run's `retention_days` (`NODE27_RAW_RETENTION_DAYS`, default 14 at
      `scripts/node27_raw_retention.py:33`). So the raw-retention default is the right constant to pin
      against; the pin's form is the inequality, not equality. The discharge tiles' own lane, the
      timeseries retention window (`packages/common/storage.py:35`, also 14), is wider in effect — with
      168 h forecast spans and `range_end <= cutoff`, a cycle's chunks survive to roughly
      `watermark − 21 d` — and is not the binding constraint. 12 satisfies `:191` with 24 h of margin
      under any anchor and independent of pipeline lag. If an operator ever sets
      `NODE27_RAW_RETENTION_DAYS < 13` the inequality breaks again; the remedy is the one
      `canonical-precip-copyback/spec.md:199-200` already names — raise `retention_days`, never lower
      this lookback.
    - **Considered and declined in round 2: anchoring on the display watermark.**
      `packages/common/display_watermark.py:3-5` states that lifecycle age on node-27 is measured from
      the display watermark and never from the host wall clock, and five consumers follow it — so the
      `now()` anchor here looks like a precedent violation and will be re-raised by anyone who reads that
      docstring. It was adjudicated and declined for four reasons. (a) It does not fix the thing it
      appears to fix: the failure mode of concern is a **coverage-refresh** stall, during which ingest
      keeps parsing, so `MAX(cycle_time)` over `hydro_run` — which is exactly what the watermark is —
      keeps advancing and a watermark-anchored window slides identically. It helps only against an
      *ingest* stall, which `scripts/node27_frontier_stall_alert.py` alerts on at 4 h per source, ~70×
      before this window could close. (b) It is unusable on the request path: `fetch_display_watermark`
      opens its own psycopg2 connection from a DSN with its own timeout, and `mvt.py` holds a
      SQLAlchemy `Session` and imports no `packages.*`; the only in-session form is a 5th round trip on
      a catalog path already at 4, in a second READ COMMITTED snapshot. (c) It is neutral to
      `canonical-precip-copyback:191` — under the watermark the constraint is exactly `L ≤ R − 1d`,
      under `now()` it is `L ≤ R − 1d + lag`, so the anchor never decides the value. (d) An in-SQL
      self-anchor (`MAX(h.cycle_time) OVER ()` over the coverage-joined rows) was also declined: it
      demotes the bound from a scan bound to a post-filter, losing the scale property the bound exists
      for, and it is invisible to `_NationalDiscoverySession`, which filters on **bound values** — the
      lookback's mutation rows would lose their red-capability.
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
      three cases that are actually about the bound monkeypatch the real value back. Rewriting every
      legacy case to relative instants is the alternative and touches far more of the decision-13
      protected files; it was rejected for that reason. The constant's own pin asserts the
      `canonical-precip-copyback:191` inequality against the real retention default
      (`NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS <= DEFAULT_RETENTION_DAYS - 1`, imported from
      `scripts.node27_raw_retention`) rather than the bare literal — round 1's pin asserted `== 14` under
      a name claiming a raw-retention coupling it never checked, which is how a value that violates
      `:191` passed a green test. The literal may be pinned alongside it, but the inequality is the
      load-bearing half.
    - Consequence, accepted: an ingest or coverage-refresh stall longer than 12 days empties `cycles[]`
      → `default_cycle = null` → the national layer renders disabled even though renderable tiles may
      still exist. That is the same fail-closed state as an empty intersection (decision 3), and dark is
      the honest state for a forecast that is 12 days old. It is a behaviour change from master, where
      the national layer resolved through the unbounded `latest_runs` CTE (no `now()` / `interval`
      lower bound anywhere in it) and therefore kept serving the last known-good covered run
      indefinitely — stale-but-online. Two qualifications
      round 2 established: an ingest stall is alerted at 4 h per source long before day 12, but a
      **coverage-refresh** stall is not alerted at all (both refresh call sites are non-fatal and the
      only stall alerter watches `hydro_run`, which keeps advancing) — that detection gap is
      pre-existing on master, is not closed by any anchor choice, and is routed out of this PR as its own
      monitoring issue.
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
      to be red-capable, `_NationalDiscoverySession.execute` (`tests/test_hydro_display_mvt_scaling.py:1479-1505`) must filter on the `since`
      bind the way it already filters on `source` and `cycle`; without that the predicate could be
      deleted and nothing would fail. The four SQL-text assertions at `:161-164` are substring `in`
      checks and are unaffected. Editing `_NationalDiscoverySession` and adding this case is the one
      exception to decision 13's "no other test is edited" — the file is already in decision 13's
      allowlist, the census in `tests/test_river_ts_read_path_surrogate_keys.py` is not touched, and the
      `h.status IN (...)` occurrence counts pinned at 2/1/1/5 by
      `tests/test_display_publish_status_only.py` MUST stay unchanged (the new predicate is on
      `h.cycle_time`, not on status, and adds no second SQL shape).

16. **The intersection compares network *sets*, not cardinalities (review round 2, r2-inv-5).** The
    denominator (`SELECT DISTINCT river_network_version_id FROM core.model_instance WHERE active_flag`)
    and the coverage query are two statements under READ COMMITTED, each with its own snapshot. Round 1
    deferred the race as "fail-closed": it only considered a network being deactivated. Round 2 found
    the minimal fail-**open** trigger is a single activation between the statements: statement 1 sees
    `{B, C1, C2}` (total 3); network A is activated and has a run for cycle K; statement 2 re-evaluates
    `active_flag` and returns `{A, C1, C2}` for K while B never had K; `3 == 3` and K is listed although
    the active network B cannot render it. Remedy: `_national_discharge_coverage_rows` returns the
    active-network **set**, and both `national_discharge_cycles` and the per-cycle branch of
    `national_discharge_valid_times` compare `covered_networks == active_networks` as sets. Without a
    race this is a behavioural no-op (same predicates, single snapshot ⇒ covered ⊆ active, so equal
    cardinality ⇔ equal sets). **Scope of the fix, stated so round 3 does not re-file it:** the set
    comparison closes the *reorder-during-activation* branch only. A network activated between the two
    statements contributes to statement 2 only for the cycles where it has rows: with **zero**
    display-ready rows it never appears at all, and with rows for K but **not** for J it is absent from
    J's covered set, so J's covered set still equals the stale active set and J stays listed although the
    newly active network cannot render it. Neither branch can be caught by the per-cycle set comparison;
    both remain the round-1 DEFER, tracked by **#2087** (statement-order swap / single-statement merge /
    REPEATABLE READ — #2017 is ops/docs scope and gives this code change no home). Slice-pin note (decision 2): the return-type
    change happens inside `_national_discharge_coverage_rows`; no new function is introduced between
    the pinned markers.

17. **The `cycle_time is None` guard in `national_discharge_cycles` stays (review round 2, C-nullbranch).**
    With the lookback bound in place, `NULL >= :since` is NULL and the SQL already drops NULL-cycle rows,
    so the Python guard is unreachable on the bound path. It is kept deliberately: `sorted()` over a dict
    keyed by `datetime | None` raises `TypeError`, so removing the guard turns any future `since=None`
    relaxation from "fail-closed drop" into an HTTP 500 on `/cycles`. Its inline comment is corrected to
    say that (it currently presents itself as the live NULL-skip oracle, which it is not; no evidence
    artifact cites it as one).

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
  (`cycles`/`valid-times` bodies + `GET /api/v1/layers` cold p95 before/after; the no-argument
  `/api/v1/layers/discharge/valid-times` row count and latency under `x-nhms-cache-warm: refresh`; the
  **effective** `retention_days` on node-27 — `NODE27_RAW_RETENTION_DAYS` if set, else the source default —
  checked against the decision-15 inequality; the integration cases named in rows 3/36/50–56 green on
  node-27, and the SQL rows' mutations measured red there in a throwaway worktree, never in the active
  checkout).

## Regression rows

| Surface + input | Expected behavior |
|---|---|
| `national_discharge_cycles(session, "gfs")`, 38 networks have cycle A, 37 have cycle B | `cycles` contains A, not B; `default_cycle == A` |
| same, three intersected cycles | `cycles` is sorted strictly descending by `cycle_time`; `default_cycle == cycles[0].cycle_time` |
| same, a cycle whose clamped window holds no 3-hour stride instant | that cycle is not listed (decision 3) |
| same, one active network has zero gfs display-ready runs | `cycles == []`, `default_cycle is None` |
| same, a network's run exists but `segment_count == 0` | that network counts as uncovered → cycle excluded |
| same, two fully-covered cycles: one inside the 12-day lookback window, one older | only the newer is listed; `default_cycle` is the newer (decision 15) |
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
| `GET /api/v1/layers` runless **and** `?run_id=<X>` | discharge entry byte-identical: template, six-tuple placeholders, `default_source == "gfs"`, same `default_cycle`, same `valid_times`, `source_refs == {}`, same `metadata.version`; `maplibre_source_layer == "hydro"`; `properties` contains `basin_id` — **receipt method (r2-int-7):** the two responses come from two independent cache entries (`layers:None:…` and `layers:<X>:…`, each with its own TTL), so on node-27 they MUST be taken back-to-back with `-H 'x-nhms-cache-warm: refresh'` on both, or the item is asserted on the hash-input fields (`default_cycle`, `valid_times`) rather than across two cached bodies; a red here without that method is a cache-skew artefact, not a defect |
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
| `GET /api/v1/layers/discharge/cycles?source=ifs` and `/valid-times?source=ifs&cycle=B`, session holds gfs rows at A and ifs rows at B | `cycles == [B]`, `source == "ifs"`; valid times for `(ifs, B)` non-empty, for `(gfs, B)` empty (rows 48/62/64/75) |
| `national_discharge_valid_times(session, source="gfs", cycle=C)`, every network covers `C+4h … C+97h` | first `C+6h`, last `C+96h` (rows 58/59) |
| same, exactly `limit` stride instants observed | `truncated is False`, `observed_count == limit` (row 57) |
| `national_discharge_valid_times(session)` (no args), zero coverage rows | `valid_times == []`, `observed_count == 0` (row 44) |
| node-27 integration (`tests/test_mvt_national_identity_probe_integration.py`): second network seeded with cycle A only, first with A and B | `cycles == [A]`, `default_cycle == A`; `valid-times(gfs, B) == []`; inactive second network does not block A; a cycle stays listed when a zero-segment rival run at the same `(network, cycle)` sorts above the complete one; a cycle older than the lookback is unlisted; `?source=ifs` lists the uppercase-`IFS` run's cycle only (rows 3/36/50–56) |

## Mutation matrix

Inherited discipline from I4: **every behavioral claim must have a red-capable oracle. A predicate
present in the SQL string but ineffective, or an argument passed but unasserted, must make at least one
test fail.** Predicate-text assertions (a statement captured from `session.executions` and matched
against a literal) are **tripwires, not oracles**: a row whose only local red is a tripwire is a
**node-27-lane row** and must also carry a node-27 `Measured` cell taken from the real-DB integration
file (`tests/test_mvt_national_identity_probe_integration.py`) with the same mutation applied in a
throwaway worktree there — without that cell the row is not measured. (This replaces the earlier
"string-shape assertions are not oracles" sentence, which row 36 was already measured through.)
**Site rule (Review Failure Retro 1, after the round-3 gate):** every site this PR adds or rewrites whose
kind is a guard / branch or ternary gate / NULL guard / empty short-circuit / comparison / set operation /
bound / SQL predicate, `PARTITION BY`, `ORDER BY`, `DISTINCT` / identity- or bound-carrying keyword
argument / cache key / fail-closed shape assignment gets **its own row**, and the Mutation cell names the
mutated `file:line` (the line is emitted by the mutation driver at apply time, not typed by hand). The
enumeration this rule was applied to is `.workplans/pr-2073/review/site-enumeration.md` (109 sites);
sites excluded from rows are listed with their reason in the block after the table. The implementer MUST
run each mutation, record the measured pass/fail counts, and extend the table rather than re-derive it.

`local` = the pytest command in decision 14 (eight files, including the three placement/SQL-shape
oracles). `Measured` is filled in by the implementer from the actual run — `local N pass / M fail` —
and is what makes the row evidence rather than an intention. Rows 1–30 (plus 4a/7a/29a/29b/29c) are the
original 35; rows 31–34 were added at review round 1 for the four fixes FIX-1..FIX-4 and rows 36/36b/37
for the cycle-dimension bound (decision 15, FIX-6); review round 2 inverted row 30 (decision 10 reversed)
and added 30b (the argument-free fallback after the FIX-2 forcing), 38 (the constant is consulted), 39
(the `:191` inequality) and 40 (set comparison, decision 16) — **35 is deliberately unused**; the
post-gate site rule added 40b/40c and 41–75 (37 rows), so the table has 83 rows with one numbering gap. Each fix pass that changes the oracle mechanism invalidates
every count taken before it (FIX-6 in round 1; the round-2 pass rewrote the pinning test behind row 30,
changed the helper's return type and added four tests — five test cases, one is parametrized ×2), so after each such pass the **whole** table is
re-measured, never spot-fixed. Every `Measured` cell below is against the post-gate baseline of
`local 372 passed` (the round-2 baseline of 365 plus the tests added by the post-gate fix pass; measured
green before the first row and again after the last restore). Rows 1–40 MUST be re-measured at that
baseline too; a cell that still reads a 365-based count is stale and the row is not measured.

|#|Claim|Mutation|Test that must go red|Measured|
|---|---|---|---|---|
|1|The intersection is an intersection, not a union|drop the `covered_networks == active_network_total` condition in `national_discharge_cycles`|partial-coverage case (cycle B) starts appearing in `cycles`|`local 369 pass / 3 fail` @ `services/tiles/mvt.py:1938`|
|2|A network with **zero** runs for the source fails the whole list closed|compute the network total from the rows returned instead of from `core.model_instance`|the "one network has no gfs run at all" case returns a non-empty `cycles`|`local 366 pass / 6 fail` @ `services/tiles/mvt.py:2058`|
|3|`segment_count > 0` is load-bearing|delete that predicate|**node-27 lane only — no local oracle.** SQL semantics: a zero-segment run then wins `rn = 1` for its `(network, cycle)`, so a **covered** cycle disappears from `cycles` (the predicate sits in the JOIN `ON` upstream of `ROW_NUMBER()`, `services/tiles/mvt.py:2041`) — it is liveness, not safety. But `_NationalDiscoverySession` (`tests/test_hydro_display_mvt_scaling.py:1466-1509`) never executes SQL; it matches on `"hydro.run_display_coverage" in sql` and returns its own canned rows, so a predicate inside the JOIN `ON` is unobservable locally for *any* row data. The local signal is the tripwire in `test_national_coverage_statements_pin_their_shape` (its `AND rdc.segment_count > 0` assertion, added after review round 4 r4-test-1 — before that the row had NO local signal at all, measured `372/0`); the oracle is the node-27 integration case `test_national_cycles_keep_a_cycle_whose_zero_segment_rival_run_sorts_first` — TWO runs at the same `(network, cycle)`: one complete, one with `segment_count = 0` whose `run_id` sorts **above** it under `ORDER BY h.run_id DESC` (`run_id` is `TEXT`, `db/migrations/000006_hydro.sql:2`). Unmutated the JOIN `ON` drops the zero-segment run and the cycle is listed; with the predicate deleted it wins `rn = 1`, `_national_coverage_window` (`mvt.py:2084`) rejects it and the covered cycle disappears. A **single** zero-segment run is not an oracle — it leaves the cycle unlisted either way|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2041` (spot re-measure after r4-test-1 added the tripwire assertion; `372/0` before it — only this row's cell can move, rows 50–56 already redden the same test) · node-27: `1 failed, 14 passed` (test_national_cycles_keep_a_cycle_whose_zero_segment_rival_run_sorts_first)|
|4|`default_cycle` is the **newest** intersected cycle|reverse the sort / take `cycles[-1]`|default-cycle assertion|`local 370 pass / 2 fail` @ `services/tiles/mvt.py:1953`|
|4a|`cycles[]` itself is descending|drop the sort (return DB/dict order)|the three-intersected-cycles ordering case|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1935`|
|5|The stride is 3 h, not 1 h|`timedelta(hours=3)` → `hours=1`|57-entry / adjacent-delta case|`local 359 pass / 13 fail` @ `services/tiles/mvt.py:108`|
|6|The list is clamped to `min(river_valid_time_end)`|drop the upper clamp|non-rectangular-coverage case|`local 369 pass / 3 fail` @ `services/tiles/mvt.py:2127`|
|7|…and to the coverage start (decision 8)|drop the lower clamp|the `one network starts at C+6h` regression case|`local 367 pass / 5 fail` @ `services/tiles/mvt.py:2126`|
|7a|`cycles[].valid_time_start` uses the same clamp as the list|clamp the list but leave `cycles[].valid_time_start = cycle_time`|the cross-endpoint consistency case|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1946`|
|8|`source` reaches the SQL bind|drop `source=` from the helper call in `national_discharge_cycles`|an ifs-only cycle appears in the gfs list|`local 370 pass / 2 fail` @ `services/tiles/mvt.py:1913`|
|9|`cycle` reaches the SQL bind|drop `cycle=` from the helper call in `national_discharge_valid_times`|the per-cycle list stops depending on the requested cycle|`local 368 pass / 4 fail` @ `services/tiles/mvt.py:1816`|
|10|The no-arg path still exists and is exercised|make `source`/`cycle` required kwargs on `national_discharge_valid_times`|the two no-arg cases in `tests/test_hydro_display_mvt_scaling.py` and the no-argument `valid-times` route case (`TypeError`). NOT the catalog — decision 10 makes it pass both kwargs|`local 364 pass / 8 fail` @ `services/tiles/mvt.py:1778`|
|11|The catalog's four new metadata fields exist and are correct|delete any one of `default_source` / `default_cycle` / `cycles_url_template` / `valid_times_url_template`|catalog shape assertion|`local 360 pass / 12 fail` @ `services/tiles/mvt.py:1480`|
|12|…and they reach the `metadata.version` hash input|remove them from `_stable_json_hash`'s dict|a test that pins the discharge `metadata.version` against a recomputed hash including them|`local 369 pass / 3 fail` @ `services/tiles/mvt.py:1489`|
|13|…and they do NOT reach non-discharge entries' hash input|add them unconditionally|`river-network` `metadata.version` moves off its pinned value|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1477`|
|14|Runless and run-scoped discharge entries are byte-identical|make `default_cycle` depend on the requested `run_id`|two-call identity assertion|`local 364 pass / 8 fail` @ `apps/api/routes/hydro_display.py:1221`|
|15|The empty intersection still returns the entry|return `None`/skip the entry when `default_cycle is None`|empty-intersection catalog case|`local 368 pass / 4 fail` @ `apps/api/routes/hydro_display.py:1195`|
|16|…and is distinct from the zero-run empty catalog|synthesize a discharge entry when `display_ready_run` is `None`|zero-run `data == []` case|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:275`|
|17|`cycle` without `source` is rejected before SQL|delete the guard|422 case (session whose `execute` raises)|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:414`|
|18|`source` without `cycle` is rejected|delete the guard|422 case|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:414`|
|19|`run_id` + `source`/`cycle` is rejected|delete the guard|422 case|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:421`|
|20|`source`/`cycle` on a non-discharge layer is rejected|delete the guard|422 case|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:407`|
|21|The source enum rejects `ERA5`/`best`/`GFS`-cased input before SQL|widen the enum check|422 case|`local 366 pass / 6 fail` @ `apps/api/routes/hydro_display.py:306`, `apps/api/routes/hydro_display.py:333`|
|22|Spellings collapse onto one cache entry|drop the canonicalization from the cache key|`test_valid_times_cache_key_collapses_spellings_and_separates_identities`. Note the discriminating spelling is the offset form (`...T20:00:00+08:00`), not the `...T12:00:00.000Z` / `...T12:00:00Z` pair this row originally named — FastAPI's datetime parsing already folds those two onto the same `datetime` before the key is built, so they cannot separate entries. The test is correct; the row's wording was not|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:349`|
|23|`(source, cycle)` separates two cache entries|drop `source` or `cycle` from the key|two-identity cache case|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:383`|
|24|Every instant is seconds-precision|emit `isoformat()` instead of `canonical_mvt_time`|regex assertion over every field|`local 349 pass / 23 fail` @ `services/tiles/mvt.py:2662`|
|25|The cycles route is in the runtime schema and the yaml|delete either the yaml block or the route|`test_static_openapi_matches_runtime_schema`|`local 363 pass / 9 fail` @ `apps/api/routes/hydro_display.py:300`|
|26|`LayerMetadata` documents the four new fields|delete them from `_layer_metadata_schema`|drift test (yaml/runtime divergence)|`local 369 pass / 3 fail` @ `apps/api/openapi_patching.py:2025`|
|27|`_layer_source_refs` refuses `discharge`|delete the `assert`|the new `AssertionError` case|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1559`|
|28|The status predicate count is untouched|add a second `h.status IN (...)` to the new helper|`tests/test_display_publish_status_only.py:192` (valid-times slice 1→2) and `:193` (module 5→6); `:187`/`:191` do NOT move|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2043`|
|29|The catalog's list is the endpoint's list|change the stride or the lower bound in one caller only (e.g. inline a second stride computation in `_default_layer_catalog`)|the catalog-vs-endpoint equality case|`local 364 pass / 8 fail` @ `apps/api/routes/hydro_display.py:1170`|
|29a|The no-arg branch still ranks latest-cycle-per-network|drop the max-`cycle_time` selection in the no-arg Python path|the `one network with two cycles` case|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1833`|
|29c|Selection happens before validation|validate all returned rows first, then select|the `older malformed cycle` case (whole discovery blanks)|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1829`|
|29b|Truncation keeps the FIRST entries in the per-cycle branch|reuse the no-arg `retained_start = end - ...` tail-keeping logic|the over-limit truncation case|`local 370 pass / 2 fail` @ `services/tiles/mvt.py:2141`|
|30|The catalog's discharge entry digests the identity it advertises — `(default_source, default_cycle)` — and the legacy alias route stays argument-free (decision 10 as reversed in round 2, r2-int-1)|revert the catalog call to the argument-free digest (or drop the `cycle=` bind)|`test_layer_catalog_digests_the_identity_it_advertises` (rewritten from `..._still_digests_every_source_not_one_identity`) + the alias-route companion assertion — a re-run of the advertised identity that changes the digest rows must change `metadata.version`|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:1184`|
|30b|With nothing advertised the catalog falls back to the **argument-free** digest, and only after the FIX-2 forcing has run (decision 10, review round 2 — the fallback branch had no kwargs pin)|always pass `source="gfs", cycle=default_cycle_instant` even when `default_cycle` is `None` (with `cycle=None` the SQL's NULL guard silently degrades to a source-only digest, so `ifs` activity stops moving `metadata.version`)|`test_layer_catalog_falls_back_to_the_argument_free_digest_when_no_cycle_is_advertised` (two cases: intersection already empty at the cycles query; intersection empties between the two snapshots — the second case alone goes red if the digest is hoisted above the forcing)|`local 370 pass / 2 fail` @ `apps/api/routes/hydro_display.py:1184`|
|31|The per-cycle branch fails closed on an off-phase hourly coverage grid — the per-cycle twin of the no-arg branch's `(common_end - start) % 3600` guard (review round 1, FIX-1: this change had dropped the guarantee on the new path)|delete the `int((window[0] - cycle).total_seconds()) % 3600` guard in `_national_cycle_valid_times`|`test_national_per_cycle_valid_times_fail_closed_for_an_off_phase_coverage_grid`|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2120`|
|32|The catalog never advertises `default_cycle = C` together with `valid_times = []` (review round 1, FIX-2: READ COMMITTED gives each of the two calls its own snapshot)|delete the `if not valid_time_sample.valid_times: default_cycle = None` forcing in `_default_layer_catalog`|`test_layer_catalog_never_advertises_a_cycle_whose_timeline_came_back_empty`|`local 370 pass / 2 fail` @ `apps/api/routes/hydro_display.py:1170`|
|33|A NULL `cycle_time` ranks FIRST in the no-arg branch, reproducing PostgreSQL's `ORDER BY ... DESC` = NULLS FIRST and the two untouched national tile CTEs (review round 1, FIX-3)|`datetime.max` sentinel → `datetime.min` in `_national_run_rank`|`test_no_argument_national_valid_times_rank_a_null_cycle_first_like_the_tile_ctes`|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2073`|
|34|The cycles cache key separates the two sources (review round 1, FIX-4: decision 11 pinned the spelling with no oracle behind it)|drop `{source}` from `f"discharge-cycles:{source}"`|`test_cycles_cache_key_separates_the_two_sources`|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:325`|
|36|The lookback predicate actually bounds the **DB scan**|drop `AND (CAST(:since AS timestamptz) IS NULL OR h.cycle_time >= :since)` from the SQL, keep the bind|**node-27 lane, same as row 3.** No local oracle: `_NationalDiscoverySession` filters by bound values and deliberately never by SQL text (`tests/test_hydro_display_mvt_scaling.py:1396-1403`, the #2009 section comment above the fake), so with the bind still passed the fake keeps filtering and nothing about the scan is observable locally. The local signal is a predicate-text assertion in `test_national_cycles_list_only_cycles_inside_the_lookback_window`, recorded below as a **tripwire, not the oracle** (preamble). The node-27 oracle is the integration case `test_national_cycles_skip_a_cycle_older_than_the_lookback`. Row 36b carries the behavioral half; the node-27 receipt (row-count + `EXPLAIN (ANALYZE, BUFFERS)` showing `hydro_run_latest_ready_run_idx`) carries the scan half|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2047` · node-27: `1 failed, 14 passed` (test_national_cycles_skip_a_cycle_older_than_the_lookback)|
|36b|…and it is actually applied — the bound value reaches the query|`national_discharge_cycles` passes `since=None`|`test_national_cycles_list_only_cycles_inside_the_lookback_window` + `test_national_cycles_are_empty_when_every_covered_cycle_predates_the_lookback` — the older cycle is listed again and the all-stale case stops being empty|`local 369 pass / 3 fail` @ `services/tiles/mvt.py:1914`|
|37|…and only `cycles` passes it — the no-arg path stays unbounded on purpose|pass the same `since` from `national_discharge_valid_times`'s no-arg branch|two tests, for two independent reasons: `test_no_argument_national_valid_times_keep_a_network_whose_newest_run_predates_the_lookback` (the intersection-membership reason) and `test_no_argument_national_valid_times_rank_a_null_cycle_first_like_the_tile_ctes` (the NULL-cycle reason below). Note the byte-identical no-arg regression case this row originally named does **not** go red — every network there has a recent newest run — so the first of the two tests had to be written for this row|`local 370 pass / 2 fail` @ `services/tiles/mvt.py:1816`|
|38|`NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS` is actually **consulted** by the query path, not merely defined (review round 2, r2-test-1 — a CONFIRMED coverage gap: hardcoding `days=14` at the call site measured `360 pass / 0 fail` on the round-2 head)|replace `days=NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS` at the `since=` call site with the literal `days=12`|`test_national_cycles_lookback_reads_the_module_constant`: monkeypatch the real constant to 3 (past the autouse widening), seed a fully-covered cycle at 1 d and another at 5 d, assert only the 1 d cycle is listed — red whenever the call site ignores the constant|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1914`|
|39|The lookback value satisfies `canonical-precip-copyback:191` against the real retention default (decision 15)|set the constant to 13 or 14|`test_national_cycle_lookback_leaves_a_day_of_precip_mirror_margin`: `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS <= DEFAULT_RETENTION_DAYS - 1` with `DEFAULT_RETENTION_DAYS` imported from `scripts.node27_raw_retention` — raising the source default stays green (allowed by `:199-200`), lowering the source default below 13 goes red; env-var / CLI overrides (`NODE27_RAW_RETENTION_DAYS`, `--retention-days`) are not observed by this test — the effective node-27 value is a receipt item, not a pytest oracle|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:128`|
|40|The intersection compares network sets, not counts (decision 16)|compare `len(covered) == len(active)` instead of the sets|`test_national_cycles_fail_closed_when_a_network_activates_between_the_two_statements`: a session whose second statement returns a covered set of equal size but different membership from the first statement's active set — the cycle must not be listed. **Measured at the `national_discharge_cycles` site only** (`services/tiles/mvt.py:1938`); the per-cycle site is row 40b|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1938`|
|40b|The per-cycle branch of `national_discharge_valid_times` also compares sets, not counts (decision 16, second site; review round 3, r3-test-1 — row 40's cell never touched this site: `364/1` at `:1938`, `365/0` at `:1825`)|`services/tiles/mvt.py:1825` → `if len(covered_networks) != len(active_networks):`|`test_national_per_cycle_valid_times_fail_closed_when_a_network_activates_between_the_two_statements`: covered `{rn-a, rn-c1, rn-c2}` for the requested cycle, active `{rn-b, rn-c1, rn-c2}` (equal size, different members) → `valid_times == []`, `observed_count == 0`; non-vacuity: the session's active set has three members|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1825`|
|40c|…and that branch fails closed instead of falling through to the clamp|`mvt.py:1826` → `return _national_cycle_valid_times(rows, cycle=cycle, limit=sample_limit)`|same test, plus `test_national_per_cycle_valid_times_are_empty_for_a_cycle_outside_the_intersection`|`local 369 pass / 3 fail` @ `services/tiles/mvt.py:1826`|
|41|Half an identity is rejected in the helper, not only at the route (decision 6)|`mvt.py:1803` → `if False:`|`test_national_valid_times_reject_half_an_identity`|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1803`|
|42|`cycle` selects the per-cycle branch|`mvt.py:1819` → `if False:`|the per-cycle 57-entry case (`tests/test_hydro_display_mvt_scaling.py:1774`) would receive the no-argument hourly grid|`local 357 pass / 15 fail` @ `services/tiles/mvt.py:1819`|
|43|The per-cycle branch honours `limit`|`mvt.py:1827` → `limit=MVT_VALID_TIME_SAMPLE_LIMIT`|the `limit=5` truncation case (`:1875`)|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1827`|
|44|The no-argument branch fails closed with zero coverage rows|`mvt.py:1835` → `if False:`|`test_no_argument_national_valid_times_are_empty_with_no_coverage_rows` (new; no existing case enters this branch)|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1835`|
|45|A malformed newest window empties the no-argument result (decision 1, ordering)|`mvt.py:1841` → `if False:`|the non-rectangular no-argument case (`:167`) — with `if False:` the `None` window reaches `mvt.py:1845` and raises `TypeError`. `:1697` and `:1893` do **not** reach this line: `:1697` is a `cycles` case (its guard is `mvt.py:2113`, row 61) and `:1893`'s winning row has a valid window|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1841`|
|46|`cycles` hands each cycle's rows to the clamp with `limit`|`mvt.py:1940` → `limit=MVT_VALID_TIME_SAMPLE_LIMIT`|**new** `test_national_cycles_pass_their_limit_to_the_per_cycle_clamp`: `national_discharge_cycles(session, source="gfs", limit=5)` over a 168 h rectangle — unmutated the entry's `valid_time_end` is `_CYCLE + 12h` (5 stride instants), mutated it is `_CYCLE + 168h`. No existing caller passes `limit` to `national_discharge_cycles`; `:1875` is on the `valid_times` path and belongs to row 43|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1940`|
|47|A cycle whose clamp yields no instant is not listed (decision 3)|`mvt.py:1941` → `if False:`|`:1735`, `:1815` (`valid_times[0]` on an empty list)|`local 369 pass / 3 fail` @ `services/tiles/mvt.py:1941`|
|48|`cycles` echoes the requested source, not a literal|`mvt.py:1951` → `"source": "gfs",`|`test_discharge_routes_pass_ifs_through_to_the_coverage_bind` (new): a session holding gfs rows at cycle A and ifs rows at cycle B; `?source=ifs` lists B only and echoes `"ifs"`|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1951`|
|49|`default_cycle` is `None`, not a sentinel, when nothing is listed|`mvt.py:1953` → `if cycles else ""`|`:1611`, `:1684`, `:1660`|`local 368 pass / 4 fail` @ `services/tiles/mvt.py:1953`|
|50|Only active networks form the denominator|`mvt.py:2004` → `WHERE TRUE`|local: tripwire `test_national_coverage_statements_pin_their_shape` (new); **node-27 oracle**: `test_national_cycles_ignore_an_inactive_network` (an inactive second `model_instance` with no runs must not block cycle A)|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2004` · node-27: `1 failed, 14 passed` (test_national_cycles_ignore_an_inactive_network)|
|51|NULL network ids are excluded from the denominator|`mvt.py:2005` → drop the `IS NOT NULL` conjunct|local: tripwire only. **No node-27 oracle is possible on this schema** — `core.model_instance.river_network_version_id` is `TEXT NOT NULL` (`db/migrations/000004_core.sql:74`) and no later migration drops the constraint, so the row the case needs cannot be seeded and the `IS NOT NULL` conjunct is unsatisfiable-by-schema. Recorded as tripwire-only with that reason, per the site rule|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2005` · node-27: `15 passed` (green by design: tripwire-only, no seedable row on a NOT NULL schema)|
|52|One ranked row per `(network, cycle)`, not per network (decision 1, ranking shape)|`mvt.py:2034` → `PARTITION BY mi.river_network_version_id`|local: tripwire; node-27: `test_national_cycles_list_only_cycles_covered_by_every_network` — network 1 has cycles A and B (B newer), network 2 has A only; partitioning by network alone lets B win `rn = 1` for network 1 and A vanishes (`cycles == []` instead of `[A]`)|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2034` · node-27: `1 failed, 14 passed` (test_national_cycles_list_only_cycles_covered_by_every_network)|
|53|The newest run per `(network, cycle)` wins|`mvt.py:2035` → `ORDER BY h.run_id ASC`|local: tripwire; node-27: `test_national_cycles_take_the_newest_run_at_a_cycle` — two runs at cycle A on one network, the older ending at `A+2h`, the newer at `A+5h`; `cycles[0].valid_time_end == A+3h`, with `ASC` it is `A`|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2035` · node-27: `1 failed, 14 passed` (test_national_cycles_take_the_newest_run_at_a_cycle)|
|54|`:source` narrows the coverage query in SQL, not only in the fake|`mvt.py:2045` → drop the conjunct, keep the bind|local: tripwire; node-27: `test_national_cycles_match_an_uppercase_source_id_from_a_lowercase_query` — the uppercase-`IFS` seed at a cycle different from the gfs one; `?source=ifs` lists the ifs cycle only|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2045` · node-27: `1 failed, 14 passed` (test_national_cycles_match_an_uppercase_source_id_from_a_lowercase_query)|
|55|`:cycle` narrows the per-cycle query in SQL|`mvt.py:2046` → drop the conjunct, keep the bind|local: tripwire; node-27: `test_national_valid_times_are_empty_for_a_cycle_outside_the_intersection` (integration twin of decision 7) — the requested cycle P must be **earlier** than the covered cycle A and must have **no run of its own on any network**: a later P is emptied by `mvt.py:2129` under the mutation too, and a P with its own `[P, P+2h]` window is emptied the same way (the ranked read still partitions by `(network, cycle)`, so P's own row wins and its window ends before A). With P run-less, the mutation lets A's rows through for P, the clamp from P lands on A and the list is non-empty — red. Plus the full-list case for A|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2046` · node-27: `1 failed, 14 passed` (test_national_valid_times_are_empty_for_a_cycle_outside_the_intersection)|
|56|`WHERE rn = 1` selects that winner|`mvt.py:2049` → `WHERE rn >= 1` (context line whose meaning changed with row 52)|local: tripwire; node-27: same case as row 53 (both runs come back, the clamp takes the shorter window)|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2049` · node-27: `1 failed, 14 passed` (test_national_cycles_take_the_newest_run_at_a_cycle)|
|57|`truncated` is strict at the limit (decision 5)|`mvt.py:2148` → `truncated=observed_count >= limit`|`test_national_per_cycle_valid_times_are_not_truncated_when_observed_equals_the_limit` (new; today's cases have 57 < 100 or 57 > 5)|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2148`|
|58|The clamp start rounds inward (ceiling)|`mvt.py:2133` → floor division|`test_national_per_cycle_valid_times_clamp_an_off_grid_window_inward` (new): window `C+4h … C+97h` → first `C+6h`, last `C+96h`; floor would advertise `C+3h`, before coverage|`local 370 pass / 2 fail` @ `services/tiles/mvt.py:2133`|
|59|The clamp end rounds inward (floor)|`mvt.py:2134` → ceiling division|same test; ceiling would advertise `C+99h`, past coverage|`local 370 pass / 2 fail` @ `services/tiles/mvt.py:2134`|
|60|The per-cycle `limit` bound is applied|`mvt.py:2139` → `retained_count = observed_count`|the `limit=5` case (`:1875`)|`local 370 pass / 2 fail` @ `services/tiles/mvt.py:2139`|
|61|A malformed window empties the per-cycle result|`mvt.py:2113` → `if False:`|`test_national_cycles_treat_a_zero_segment_run_as_uncovered` (`:1697`)|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:2113`|
|62|The cycles route passes the requested source through|`apps/api/routes/hydro_display.py:323` → `source="gfs"`|row 48's new test, through the route|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:323`|
|63|The valid-times route takes the identity branch only with both parameters|`hydro_display.py:353` → `if False:`|`:2290`|`local 369 pass / 3 fail` @ `apps/api/routes/hydro_display.py:353`|
|64|…and passes `source` through|`hydro_display.py:354` → `source="gfs"`|row 48's new test (`/valid-times?source=ifs&cycle=B` is non-empty; under gfs it is empty)|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:354`|
|65|…and `cycle` through|`hydro_display.py:354` → a fixed instant|`:2290`|`local 369 pass / 3 fail` @ `apps/api/routes/hydro_display.py:354`|
|66|The valid-times cache key carries the cycle (decision 11)|`hydro_display.py:383` → drop `{cycle_key}`|`:2333` (`len(set(keys)) == 3`)|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:383`|
|67|A request with neither parameter short-circuits before validation|`hydro_display.py:404` → `or`|`:2425` (`cycle-without-source`, `source-without-cycle`)|`local 370 pass / 2 fail` @ `apps/api/routes/hydro_display.py:404`|
|68|Sub-second cycles are rejected (decision 6)|`hydro_display.py:428` → `return cycle`|`:2425` (`sub-second-cycle`)|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:428`|
|69|The catalog asks `cycles` for the default source|`hydro_display.py:1146` → `source="ifs"`|`:2230`|`local 370 pass / 2 fail` @ `apps/api/routes/hydro_display.py:1146`|
|70|The catalog parses `default_cycle` only when present|`hydro_display.py:1152` → unconditional `datetime.fromisoformat(default_cycle)`|`:2141`, `:1264` (`intersection-already-empty-at-the-cycles-query`)|`local 370 pass / 2 fail` @ `apps/api/routes/hydro_display.py:1152`|
|71|The catalog asks for valid times only with an identity|`hydro_display.py:1153-1155` → always take the identity branch|`:2141`|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:1155`|
|72|…with the default source|`hydro_display.py:1158` → `source="ifs"`|`:2098` (asserts `metadata["default_cycle"] == "2026-09-02T12:00:00Z"` at `:2124` and `len(metadata["valid_times"]) == 57` at `:2125`; under `source="ifs"` the sample empties and `hydro_display.py:1170-1172` forces `default_cycle` back to `None`) and `:2230`. **Not** `:2141` — its `default_cycle_instant` is `None`, so `:1153-1155` takes `_empty_valid_times()` and this line never runs|`local 370 pass / 2 fail` @ `apps/api/routes/hydro_display.py:1158`|
|73|…and the default cycle|`hydro_display.py:1159` → a fixed instant|`:2098` — a fixed instant other than `_CYCLE` empties the coverage rows, `hydro_display.py:1170-1172` forces `default_cycle` back to `None`, and `:2124` / `:2125` go red. **Not** `:2141`, which never evaluates `:1159` (same reason as row 72). The mutation's fixed instant must differ from `_CYCLE`|`local 370 pass / 2 fail` @ `apps/api/routes/hydro_display.py:1159`|
|74|A caller-supplied digest is not recomputed|`hydro_display.py:1173` → drop the guard|`tests/test_api_contract.py::test_run_scoped_layer_catalog_keeps_discharge_national_source_refs` — measured red: `test_run_scoped_layer_catalog_keeps_discharge_national_source_refs`|`local 371 pass / 1 fail` @ `apps/api/routes/hydro_display.py:1173`|
|75|`national_discharge_valid_times` forwards `source` to the coverage query|`mvt.py:1816` → drop `source=source`|row 48's new test at the helper level, **negative half only**: `national_discharge_valid_times(session, source="gfs", cycle=B)` must stay `valid_times == []` / `observed_count == 0`. Dropping `source=source` passes `None`, which the SQL and the fake both read as "no filter" (`mvt.py:2045`, `tests/test_hydro_display_mvt_scaling.py:1493`), so the ifs rows at B satisfy the set comparison at `:1825` and the call comes back non-empty. The `(ifs, B)` non-empty assertion stays green under this mutation and is **not** the oracle|`local 371 pass / 1 fail` @ `services/tiles/mvt.py:1816`|

### Sites without a row (site rule exclusions, `.workplans/pr-2073/review/site-enumeration.md` numbering)

- **Response-shape literals and templates** (enumeration 1, 4–8, 11, 14, 81): pinned field-by-field by `test_national_discharge_metadata_advertises_exactly_one_identity` (`tests/test_hydro_display_mvt_scaling.py:1987`) and the OpenAPI equality test; changing a literal is a contract change, not a guard.
- **`"valid_time_end": discovery.valid_times[-1]`** (46, `mvt.py:1947`): already red-capable without a new row — `test_national_cycles_and_valid_times_agree_on_every_listed_window` (`:1752`) pins both endpoints against the list the valid-times endpoint serves (`:1769`, `:1771`).
- **Signature defaults / removed keyword** (9, 82, 88, 100): compile- or contract-level; 82's removal is pinned by `tests/test_hydro_display_mvt_scaling.py:1247` (`assert len(recorded) == 1`, inside `test_layer_catalog_digests_the_identity_it_advertises` at `:1173`), 88 by the same file's `:2425[unshaped-cycle]`.
- **The bind dict** (62, `mvt.py:2053`): dropping any key makes SQLAlchemy raise before execution — compile-level, no behavioural row needed.
- **SQL projections** (55, `mvt.py:2018` / `:2026`): node-27 lane, NOT compile-level. `text()` does not validate column lists — dropping the inner `h.cycle_time` raises only at PostgreSQL execute time, and dropping the outer `cycle_time` raises nothing at all: every row then reads `cycle_time is None` at `mvt.py:1919` and `cycles` silently empties. Both are caught transitively by any node-27 cycles case that asserts a non-empty `cycles` (rows 50–56); locally the fake never executes SQL, so neither is observable.
- **Relocated from master, byte-equivalent logic** (30–34, 65–67): not written by this PR; their pre-existing oracles (`:92`, `:167`, `:1628`) are unchanged (site 67's `end < start` half has no oracle even on master; the exclusion rests on relocation, not on coverage).
- **`sample_limit = max(0, limit)`** (18, 35): no route or catalog call site passes `limit` at all (`hydro_display.py:323/354/356/1146/1156`), so it is always the module constant `MVT_VALID_TIME_SAMPLE_LIMIT`; the only explicit value anywhere in the repo is the positive `limit=5` at `tests/test_hydro_display_mvt_scaling.py:1879`.
- **`cycle_time is None` guard** (38, `mvt.py:1919`): unreachable by construction on the bound path (decision 17); kept as a `TypeError` fence, not an oracle.
- **Defensive duplicates** (74 `mvt.py:2129`, 77 `mvt.py:2135`): each mutation is result-equivalent — the other check yields `observed_count = 0` and an empty list.
- **`if not windows`** (70, `mvt.py:2123`): with at least one active network the per-cycle branch stops empty row sets at `mvt.py:1825` first (rows 40b/40c). The one residual entry is **zero active networks** — `frozenset() == frozenset()` passes `:1825` and `_national_cycle_valid_times([], …)` reaches this line, where deleting the guard turns the empty result into a `ValueError` at `:2126` (HTTP 500 instead of `valid_times: []`). Excluded as a failure-MODE-only difference on a state node-27 only holds before any `model_instance` is activated; recorded rather than dropped.
- **`DISTINCT` / `ORDER BY` on the active-network statement, `ORDER BY` on the ranked read** (50, 53, 61): result-equivalent — the Python side collapses into a `frozenset` and sorts descending itself (row 4a). Only site 50 (`SELECT DISTINCT`) is additionally pinned as statement shape by the tripwire; sites 53 and 61 are unpinned and rest on result-equivalence alone.
