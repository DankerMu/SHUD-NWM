# Design — fix-national-digest-cache-identity-2031

Fixture level: expanded (mandatory triggers: `schema` — new migration column; shared cache-identity helper
feeding two public tile layers; geospatial data). Repair intensity: **high** (shared helper behaviour +
stale-state/cache identity across producer, storage and public routes). Project profile: NHMS
(`openspec/project-profile.md`). Upstream suggested level: absent (hand-written issue from a PR #2027
review deferral; `Readiness: needs-triage` — the triage is the node-27 measurement recorded in
`docs/runbooks/receipts/2026-09-08-issue-2031-digest-precondition.md`).

## Change surface

- `services/tiles/mvt.py::national_discharge_source_version` — new kw-only `valid_time: datetime | None = None`;
  ranked sub-query gains `JOIN core.river_network_version rnv ON rnv.river_network_version_id = mi.river_network_version_id`,
  projects `rnv.geometry_generation`, and applies the NULL-guarded coverage-window predicate.
- `services/tiles/mvt.py::national_river_network_source_version` — projects `rnv.geometry_generation`.
- `apps/api/routes/hydro_display.py` — `hydro_national_source_cycle_mvt_tile` and `hydro_national_mvt_tile`
  pass `valid_time=` to the digest; `_default_layer_catalog` keeps calling without it.
- `workers/model_registry/basins_registry_import.py::_backfill_output_segment_geometry` — bumps
  `core.river_network_version.geometry_generation` iff `len(updated_rows) > 0`, same cursor.
- `db/migrations/000057_river_network_version_geometry_generation.sql`.
- Unchanged callers of the backfill (the bump lives inside the callee): `workers/model_registry/basins_registry_import.py::_import_basin`, `workers/model_registry/qhh_production_bootstrap.py` (two call sites), `scripts/node27_autopipeline.py::_backfill_output_geometry` (node-27 live path, `with conn:` commit), `scripts/reingest_all_basins_receipt.py`.

## Must preserve

- Tile bytes and 200/424 verdict of both national routes for every input (run selection lives in
  `postgis_tile_sql`, untouched). The three legacy cases in `tests/test_mvt_national_identity_probe_integration.py`
  stay green unchanged.
- `national_discharge_source_version(session)` with no arguments still ranks each network's OVERALL-latest
  run (`unbound == late` in `test_national_digest_narrows_the_ranked_runs_to_the_bound_identity`).
- `/api/v1/layers` digest call stays identity-only (`source`/`cycle`, no `valid_time`); matrix row 20 of
  i4-2007 unchanged.
- Exactly one `h.status IN (...)` in the digest, five in the module (`tests/test_display_publish_status_only.py:191-196`).
- `NATIONAL_DISCHARGE_QUERY_VERSION == "fair-network-budget-v5"`, `NATIONAL_RIVER_NETWORK_QUERY_VERSION == "stream-type-aggregate-v3"` (pinned by the sibling change's spec + tests).
- Import idempotency: `_refresh_parent_version_materialization` keeps writing `segment_count/source_uri/checksum` only; `geometry_generation` is never reset by import and never compared by `CHECKSUM_CONFLICT`.
- Tile publisher / sqlite harness (`tests/test_tile_publisher.py`) — unaffected unless it calls a national digest (implementer confirms by grep).

## Must add/change

- Digest–tile run agreement at a bound instant (A).
- Geometry generation column + write-side bump + projection in both national digests (B).
- Migration is idempotent (`ADD COLUMN IF NOT EXISTS`) and applied by `apply_migrations_from_zero` in the integration fixture.

## Decisions

- **D1 — A is fixed in the digest SQL, not by version bumps / manual purge.** Measurement: legacy divergence reachable on 38/38 networks; new-route double runs exist (20 groups) and only need differing windows. A manual `QUERY_VERSION` bump is a full cold miss and does not close the next divergence.
- **D2 — Predicate text.** `AND (CAST(:valid_time AS timestamptz) IS NULL OR (rdc.river_valid_time_start <= :valid_time AND rdc.river_valid_time_end >= :valid_time))` in the ranked sub-query's WHERE, CAST form per i4-2007 decision 1 (no `::text`-style phantom binds). This is **not byte-identical** to the tile CTE's unguarded `JOIN … ON` predicate — recorded deviation from tasks.md 3.1's "逐字一致"; the oracle is behavioural (same run when bound), not string equality. Always bind `:valid_time` (None when absent) — SQLAlchemy `text()` requires every declared bind.
- **D3 — Both tile routes pass `valid_time`; catalog does not.** The catalog has no instant; passing none keeps i4-2007 row 20. The legacy route passes `valid_time` with `source`/`cycle` still `None` — its *bytes* are unchanged, but every legacy tile whose instant precedes the latest run's window gets a new key once (Q3: all 38 networks). Same class of accepted rotation as the v5 bump in i4-2007.
- **D4 — B uses an explicit `geometry_generation` column, not `checksum`.** `checksum` is written from the source geometry package and compared by `CHECKSUM_CONFLICT` (`basins_registry_import.py`); rewriting it on backfill would make the next import misreport a conflict. A digest over `core.river_segment.geom` per request is rejected (full-network scan on every tile request).
- **D5 — Zero-row backfill must not bump.** `_backfill_output_segment_geometry` returns 0 at two early exits and `ST_Length(source.geom) > 0` can empty `updated_rows` while `updates` is non-empty; the bump is tied to `len(updated_rows) > 0`. Without this every bootstrap tick with `only_missing=True` on a complete network would rotate every national key (Q5: all 38 networks are complete today).
- **D6 — Deploy order is a documented prerequisite, not executed in this change (tracked as issue #2145).** Code referencing `geometry_generation` before `000057` is applied fails on both sides: the read side (three national tile routes — the two hydro-national routes plus `river_network_national_mvt_tile` — and `/api/v1/layers`) 500s, and the write side (`_backfill_output_segment_geometry`'s generation bump, reached by the `nhms-node27-autopipe.timer` on both arms — the new-basin seed via `import-basins-registry` with the default backfill, and the already-seeded display-ready arm `_ensure_seeded_basin_display_ready` → `_backfill_output_geometry(only_missing=True)` — and by `qhh_production_bootstrap.py` with `only_missing=False`) raises `UndefinedColumn` and rolls the enclosing transaction back (tick records `seed_failed` with `stage=import` or `stage=display_ready`). The write side goes live on `git pull --ff-only` alone, with no restart to gate it, so the migration is applied in the same window as the pull — before the next timer tick or any bootstrap — and the display API is restarted after it. Migration first (`000057` specifically is idempotent and safe ahead of code: an unread column), restart second. Dormant on geometry-complete networks (`only_missing=True` paths return 0 before the bump; receipt Q5: all 38 networks complete). No DDL is run on node-27 production in this workflow; B's node-27 receipt is the real-DB integration test on a throwaway database (oracle table: "DB migration → node-27 real-DB pytest"). Known limit: no live display receipt until the migration is applied; the deploy step (apply `000057`, restart display API, C1–C4-style receipt for the three national tile routes + `/api/v1/layers`) is owned by the node-27 operator and tracked as issue #2145, listed in the Evidence Floor.
- **D7 — No `QUERY_VERSION` bumps.** The projection change (new `geometry_generation` field in the JSON basis) already rotates every national key once; the literals are pinned by `specs/mvt-tile-contract` (sibling change) and `test_national_discharge_query_version_is_pinned_to_the_literal_the_spec_names` (`tests/test_hydro_display_mvt_scaling.py`, `test_national_discharge_query_version_is_pinned_to_the_literal_the_spec_names`; the 4.4 tests are inserted above it, so its line number moved from `:616` to ~`:789`) and the `-v3` prefix assertion at `:220`.
- **D8 — Digest gains `JOIN core.river_network_version rnv`.** `core.model_instance.river_network_version_id` is `NOT NULL REFERENCES core.river_network_version` (`db/migrations/000004_core.sql:74`), so the INNER JOIN drops no candidate; it also aligns the digest's join shape with `latest_runs`.

## Seams under test

- `national_discharge_source_version(session, *, source, cycle, valid_time)` return value (the digest string) — behavioural equality/inequality across seeded runs on a real database.
- `national_river_network_source_version(session)` return value.
- `_backfill_output_segment_geometry(cursor, river_network_version_id, *, only_missing)` — its return value and the SQL it issues (fake cursor) plus real-DB effect on `geometry_generation` and both digests.
- Route → digest call boundary (`hydro_display` monkeypatched digest helper records kwargs).
- Migration file applied from zero by `tests/integration_helpers.apply_migrations_from_zero`.

## Selected risk packs

- Schema / columns / units / field names: migration adds `geometry_generation INTEGER NOT NULL DEFAULT 0`; both digests reference it; sqlite/fake harnesses that build `core.river_network_version` must not break.
- Concurrency / shared state / ordering (stale state): cache identity vs data — the governing invariant below; zero-row bump guard (D5).
- Legacy compatibility / examples: legacy route bytes/verdict unchanged; unbound digest question unchanged; catalog call unchanged.
- Data-integrity (domain: Published NHMS artifacts / display identity): digest describes what the tile reads; write-side bump in the backfill transaction.
- Domain: PostGIS / TimescaleDB behaviour (coverage-window predicate on `hydro.run_display_coverage`); Geospatial / basin geometry (backfill moves `geom` + STORED `stream_type`).

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — no route signature, OpenAPI, or CLI change (kwarg on an internal helper only).
- Config / project setup: not selected — no env/config change.
- File IO / path safety / overwrite: not selected — file-cache path derivation unchanged; only the key input changes.
- Schema / columns / units / field names: **selected** (above).
- Auth / permissions / secrets: not selected — same roles; `nhms_display_ro` only reads the new column.
- Concurrency / shared state / ordering: **selected** (stale cache identity, zero-row guard).
- Resource limits / large input / discovery: not selected — digest row count unchanged (still rank-1 per network); one extra small-table join.
- Legacy compatibility / examples: **selected**.
- Error handling / rollback / partial outputs: not selected beyond D5 — the bump shares the backfill's transaction, so a rollback drops both.
- Release / packaging / dependency compatibility: not selected — no dependency change; deploy ordering handled by D6 documentation.
- Documentation / migration notes: **selected** — receipt, matrix appendix, deploy-order line in the PR body and runbook.

Domain packs (profile): Published NHMS artifacts / display identity — selected; PostGIS/Timescale — selected; Geospatial/basin geometry — selected; Hydro-met forcing windows, SHUD runtime, Slurm, providers, run-manifest/QC — not selected (untouched).

## Invariant Matrix

- Governing invariant: for every national tile request `(source?, cycle?, variable, valid_time, z/x/y)`, the digest embedded in `source_version` changes whenever (a) the run the tile SQL would select for that request changes, or (b) `_backfill_output_segment_geometry` rewrites that network's `core.river_segment.geom`/`stream_type` in place — the function holding the only `UPDATE core.river_segment` statement in production code (pinned by a source-scan test); and it does not change when neither happened (no spurious rotation). Row-level INSERT/DELETE of river segments and the `_seed_output_segment_rows` upsert are outside this invariant (Non-goals).
- Source-of-truth identity/contract: the digest row basis — per active network the rank-1 run under `(source, cycle, valid_time)` predicates (`run_id, cycle_time, updated_at, coverage window, sample count`) plus `core.river_network_version.geometry_generation`; hashed by `_national_source_digest`.
- Producers: `hydro.hydro_run` / `hydro.run_display_coverage` writers (unchanged); `_backfill_output_segment_geometry` (bumps generation); migration `000057` (column birth, default 0).
- Validators/preflight: `national_discharge_source_version` predicate set (status, active instance, network not null, source/cycle, valid_time); `_backfill_output_segment_geometry` `ST_Length > 0` filter and early exits.
- Storage/cache/query: `cache_key(TileInput)`, `_file_cache_path`, `map.tile_cache` read/upsert; `postgis_tile_sql("hydro-national")` `latest_runs` + identity probe (unchanged, the reference run selection).
- Public routes/entrypoints: `hydro_national_source_cycle_mvt_tile`, `hydro_national_mvt_tile` (pass `valid_time`); `river_network_national_mvt_tile` (consumes `national_river_network_source_version`, call shape unchanged, key rotates once from the projection); `/api/v1/layers` (`_default_layer_catalog`, unchanged call shape).
- Frontend/downstream consumers: none changed — key rotation is server-side; ETag is bytes-only.
- Failure paths/rollback/stale state: zero-row backfill (D5) → no rotation; backfill transaction rollback → generation bump rolls back with the geometry; `:valid_time` unbound → overall-latest question; code deployed before migration → read side 500 / write side `UndefinedColumn` + rollback (D6, documented order).
- Evidence/audit/readiness: receipt `2026-09-08-issue-2031-digest-precondition.md`; i4-2007 matrix appendix; node-27 real-DB pytest output in the PR.
- Regression rows:
  - Two display-ready runs at the same `(gfs, cycle)` on one network, rival with the higher `run_id` and a window that excludes instant T → digest bound to T equals the pre-rival digest bound to T (the rival is not a candidate at T); digest bound to an instant both cover differs from its pre-rival value; tile at T is 200 with unchanged bytes.
  - Same seed, digest called with no arguments → moves when the rival appears (overall-latest question unchanged).
  - `_backfill_output_segment_geometry` updates ≥1 row → `geometry_generation` +1, both national digests differ from before; a second `only_missing=True` pass on the now-complete network → returns 0, generation unchanged, both digests byte-identical.
  - Fake cursor: non-empty `updates` but `execute_values` returns `[]` → no generation UPDATE issued.
  - Legacy route: three existing probe cases (424 no run / 424 interior gap / 200 with data) unchanged.
  - Catalog: `test_layer_catalog_digests_the_identity_it_advertises` unchanged; a new assertion that the catalog call passes no `valid_time`.
  - Unbound digest SQL still satisfies `tests/test_hydro_display_mvt_scaling.py:82-90`.

## Boundary-surface checklist (high)

- Shared helper roots: `services/tiles/mvt.py` national digests; `_national_source_digest` (unchanged).
- Public entrypoints: two national tile routes; `/api/v1/layers`.
- Read surfaces: `postgis_tile_sql("hydro-national")` (reference, unchanged); `tile_segments` / `network_stream_max` (geometry readers, unchanged).
- Write/delete/overwrite surfaces: `_backfill_output_segment_geometry` (+ generation bump; all callers unchanged, see Change surface); migration 000057. Row-level `_ensure_river_segments` INSERT / `_delete_legacy_seg_rows` DELETE: out of scope (Non-goals).
- Staging/publish/rollback: backfill transaction (bump inside it).
- Producer/consumer evidence boundaries: receipt + matrix appendix + node-27 pytest output.
- Stale-state/idempotency boundaries: D5 zero-row guard; `_refresh_parent_version_materialization` must not touch the column.
- Unchanged downstream consumers: frontend, ETag, `national_discharge_valid_times`, per-basin `river-network` layer, `hydro` single-run layer.

## Non-goals

- No change to `postgis_tile_sql` run selection, the 424 probe, or the `(source, cycle)` binding.
- No cache TTL / purge design (would touch all five layers and both cache tiers; separate issue).
- No frontend change; no OpenAPI change.
- `national_discharge_valid_times`' ranked sub-query is left as is: it computes coverage windows, not a per-instant run.
- Row-level river-segment INSERT/DELETE paths (`_ensure_river_segments`, `_delete_legacy_seg_rows`) and imports run with `backfill_output_segment_geometry=False` (`_import_basin` flag, `scripts/reingest_all_basins_receipt.py`, `qhh_production_bootstrap.py:1031`) do not touch `geometry_generation`. They create or remove rows under a network version rather than rewriting them in place; today they run inside a per-basin import that also refreshes `segment_count`/`checksum` (river-network digest) and typically lands a new/active `model_instance` (discharge digest rows move). Covering them is a separate change; recorded here so the invariant is not read as "every geometry write".
- `workers/model_registry/qhh_production_bootstrap.py::_seed_output_segment_rows` (`INSERT … ON CONFLICT (river_segment_id, river_network_version_id) DO UPDATE SET segment_order, properties_json`) rewrites `properties_json` in place on existing output rows — and therefore the STORED `stream_type` — without bumping `geometry_generation` itself. It is covered today only by call-site ordering: both callers (`qhh_production_bootstrap.py:686→:694` and `:1068→:1076`) run `_backfill_output_segment_geometry(cursor, …)` with `only_missing=False` on the same cursor immediately afterwards, which restores `Type` and bumps when ≥1 row updates. Residual, recorded not fixed: if that trailing backfill returns 0 (no reach rows, or every candidate dropped by `ST_Length > 0`), the wipe lands with no rotation, and neither entry fails closed on it — the standalone `seed_qhh_output_segments` and the bootstrap's `_assert_complete_qhh_output_segment_geometry` both consume `_qhh_output_segment_geometry_counts`, which counts `geom IS NULL` only, and the upsert never touches `geom`. Task 4.5's scan pins this upsert as the single `ON CONFLICT … DO UPDATE` on `core.river_segment` AND pins its `SET` list to exactly `segment_order, properties_json`, so adding `geom = EXCLUDED.geom` to it reddens rather than slipping past the `UPDATE`-only pin.
- Not applying `000057` on node-27 production in this workflow (deploy step, D6).
- Lock ordering of the generation bump (round-1 finding, CONFIRMED/DEFER): `_import_basin` locks the `core.river_network_version` row first (`_refresh_parent_version_materialization`) and touches segment rows later, while a backfill-only transaction (`scripts/node27_autopipeline.py::_backfill_output_geometry`, `seed_qhh_output_segments`) now locks segment rows first and the network row second — an ABBA pair that Postgres resolves with `deadlock detected` on one side (atomic rollback, retried by the next tick / rerun; the display read path takes neither lock). Reachable only while a network has committed, fillable NULL-geom output rows (transient, post-bootstrap; receipt Q5: none today). Not fixed here: no lock-ordering invariant in this change's matrix; tracked as issue #2157.
- Not reconciling the stale `basins-registry-import` spec text that says `_backfill_output_segment_geometry` was removed (pre-existing spec/code drift; tracked as issue #2155, not fixed here).
- Not adding `map.tile_cache` purge for the file cache; not fixing that the DB tile cache is empty/unused under `nhms_display_ro` on node-27 (measurement posted on issue #2032). The per-basin `river-network` and single-run `hydro` digests have the same geometry blindness this change closes for the national layers — tracked as issue #2156. The remaining enforcement gaps around `geometry_generation` (the `_seed_output_segment_rows` residual above, no pin on `_refresh_parent_version_materialization`/`_ensure_river_network`, scan excludes `db/`, `only_missing=False` over-rotation, row-level INSERT/DELETE paths) are bundled in issue #2154.

## Review focus

- The bound digest ranks exactly the run `latest_runs` would pick; the unbound question is unchanged; no second `h.status IN`.
- Both tile routes pass `valid_time`; the catalog call does not.
- Generation bump only on `len(updated_rows) > 0`, same cursor/transaction; import refresh never resets it.
- Migration idempotent, picked up by `apply_migrations_from_zero`; no harness builds `core.river_network_version` without the column and then calls a national digest.
- Deploy-order note present in PR body/runbook; no `QUERY_VERSION` bump.
