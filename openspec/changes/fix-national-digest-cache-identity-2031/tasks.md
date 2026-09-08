# Tasks — fix-national-digest-cache-identity-2031

Fixture level: expanded · repair intensity: high · seats round 1: 4 (`correctness`, `invariant-state`,
`test-evidence+spec-compliance`, `security-perf+integration`).

## 1. Measurement + ruling (orchestrator, done before implementation)

- [x] 1.1 node-27 read-only measurement of A's precondition (Q1–Q6) → `docs/runbooks/receipts/2026-09-08-issue-2031-digest-precondition.md`.
- [x] 1.2 Ruling recorded: A fixed in digest SQL (D1–D3); B via `geometry_generation` column (D4–D5); deploy order documented (D6); no version bumps (D7). Written into `design.md` and the i4-2007 matrix appendix.

## 2. Migration (B)

- [x] 2.1 `db/migrations/000057_river_network_version_geometry_generation.sql`: header comment (why, #2031), `ALTER TABLE core.river_network_version ADD COLUMN IF NOT EXISTS geometry_generation INTEGER NOT NULL DEFAULT 0;`. Re-runnable.
- [x] 2.2 Unit test that the migration file exists, is the next number, and its statements are idempotent (`IF NOT EXISTS`) — same style as existing migration tests; `apply_migrations_from_zero` picks it up in the integration fixture (no code needed).

## 3. Write side (B)

- [x] 3.1 `_backfill_output_segment_geometry`: after `updated_rows = execute_values(...)`, `if updated_rows:` issue `UPDATE core.river_network_version SET geometry_generation = geometry_generation + 1 WHERE river_network_version_id = %s` on the same cursor. Early-exit paths (`not reaches_by_index`, `not updates`) and an empty `updated_rows` issue no UPDATE. Return value unchanged.
- [x] 3.2 Unit tests (`tests/test_hhe_mvt_binding.py` fake-cursor style): (a) ≥1 updated row → generation UPDATE issued once, after the segment UPDATE; (b) `updates` non-empty but `execute_values` returns `[]` → no generation UPDATE; (c) `only_missing=True` on a complete network → no UPDATE at all (existing case extended).
- [x] 3.3 Confirm `_refresh_parent_version_materialization` and `_ensure_river_network` do not write `geometry_generation` (grep; no change expected — report).

## 4. Digests (A + B)

- [x] 4.1 `national_discharge_source_version(session, *, source=None, cycle=None, valid_time=None)`: ranked sub-query adds `JOIN core.river_network_version rnv ON rnv.river_network_version_id = mi.river_network_version_id`, projects `rnv.geometry_generation`, and adds the D2 predicate. Bind `valid_time` always (None allowed). Keep exactly one `h.status IN (...)`.
- [x] 4.2 `national_river_network_source_version`: add `rnv.geometry_generation` to the `SELECT DISTINCT` projection (sqlite branch untouched otherwise).
- [x] 4.3 Routes: `hydro_national_source_cycle_mvt_tile` passes `valid_time=valid_time_instant`; `hydro_national_mvt_tile` passes `valid_time=valid_time` (source/cycle stay None); `_default_layer_catalog` unchanged.
- [x] 4.4 Unit tests (`tests/test_hydro_display_mvt_scaling.py`): digest SQL contains the guarded coverage predicate, `geometry_generation`, and the rnv join; a changed `geometry_generation` value in a fake row moves both digests; `test_each_national_route_hands_the_digest_helper_its_own_identity` extended — both tile routes pass `valid_time`, catalog passes none; existing shape assertions at `:82-90` and the version-literal pins (`:220`, `:616-625`) stay green unchanged.

- [x] 4.5 Source-scan test (style of `tests/test_display_publish_status_only.py`): production code under `apps/ services/ workers/ packages/ scripts/` contains exactly one `UPDATE core.river_segment` statement and it is inside `_backfill_output_segment_geometry`; exactly one `INSERT … ON CONFLICT … DO UPDATE` targeting `core.river_segment` and it is inside `qhh_production_bootstrap.py::_seed_output_segment_rows`, and that upsert's `DO UPDATE SET` column list is exactly `segment_order, properties_json` (`geom` absent — a regex assertion on the SET clause, so adding `geom = EXCLUDED.geom` to the existing upsert or adding a new upsert both redden); the generation bump SQL appears exactly once in the module.

## 5. Real-DB integration (node-27 oracle)

- [x] 5.1 A divergence case in `tests/test_mvt_national_identity_probe_integration.py` (or a sibling file reusing its fixture/helpers; the three legacy cases must stay untouched): baseline `digest(gfs, cycle, valid_time=_WINDOW_END)`; seed a rival at the SAME cycle with a lexically greater `run_id` and window `[cycle, cycle+1h]`; refresh coverage for both; assert digest at `_WINDOW_END` == baseline, digest at `cycle+1h` != its pre-rival value, unbound digest moved, tile at `_WINDOW_END` still 200 with the same bytes as before seeding.
- [x] 5.2 B case: seed output-river rows with NULL geom + matching reach rows, run `_backfill_output_segment_geometry` (psycopg cursor, committed) → `geometry_generation` 0→1, both national digests differ from before; second pass `only_missing=True` → returns 0, generation stays 1, digests unchanged.
- [x] 5.3 Red proof: 5.1 and 4.4's route assertion must fail against pre-change `services/tiles/mvt.py` / `hydro_display.py`; 5.2 and 3.2(a) must fail against pre-change `basins_registry_import.py` (batched red-proof stash, popped immediately).

## 6. Docs / contract sync

- [x] 6.1 Deploy-order line (migration `000057` before display API restart; safe ahead of code) in `docs/runbooks/node-27-bringup-checklist.md` (or the display deploy runbook the implementer finds) and in the PR body.
- [x] 6.2 i4-2007 invariant matrix appendix with the ruling and the superseded-only-for-`valid_time` note on task 3.1 (orchestrator).
- [x] 6.3 `openspec validate fix-national-digest-cache-identity-2031 --strict --no-interactive` green.

## Evidence Floor (issue #2031 acceptance criteria → evidence)

| Criterion | Evidence |
|---|---|
| node-27 read-only measurement of A's precondition, with SQL + output | receipt `2026-09-08-issue-2031-digest-precondition.md` |
| Ruling recorded (fix A in digest SQL + sync 3.1 / matrix / test contract) | design.md D1–D3, matrix appendix, tasks 4.x |
| If A fixed: digest and `latest_runs` pick the same run for one `(source, cycle, valid_time)`; two same-cycle runs with different windows | 5.1 on node-27 (pytest output in PR) |
| If A fixed: legacy route 200/424 and bytes unchanged | three existing probe cases green on node-27; 5.1 byte assertion |
| B: backfill updating ≥1 row changes a national digest — unit + node-27 receipt | 3.2(a) local; 5.2 node-27 |
| B covers `national_river_network_source_version` too | 4.2 + 5.2 asserts both digests |
| Conclusions written back to i4-2007 matrix | 6.2 |
| Profile matrix row "Display/API → node-27 live receipt" | **Deferred to the deploy step**: apply `000057` on node-27, restart display API, receipt for both national tile routes + `/api/v1/layers` (200 / expected 424, no 500). Owner: node-27 operator; tracked issue #2145. Not satisfiable in this PR without production DDL (design.md D6). |

## Risk pack → evidence

- Schema/columns: 2.1, 2.2, 4.1, 4.2, 5.2 (column applied from zero and read by both digests).
- Concurrency/stale state: 5.1, 5.2 (second pass), 3.2(b).
- Legacy compatibility: legacy probe cases + 4.4 catalog/unbound assertions + version-literal pins.
- Data-integrity / display identity (domain): 5.1, 5.2.
- PostGIS/Timescale (domain): coverage predicate exercised on a real database in 5.1.
- Geospatial / basin geometry (domain): 5.2 (real PostGIS backfill moves `geom` + STORED `stream_type` and rotates both digests) + 3.2 + 4.5.
- Documentation/migration notes: 6.1, 6.2, receipt.

## Verification commands

- Local: `uv run ruff check .`; `uv run pytest -q tests/test_hydro_display_mvt_scaling.py tests/test_hhe_mvt_binding.py tests/test_display_publish_status_only.py tests/test_basins_registry_import.py tests/test_api_contract.py tests/test_openapi_drift.py <migration test file>`; `openspec validate fix-national-digest-cache-identity-2031 --strict --no-interactive`.
- node-27: `cd /home/nwm/NWM && git pull --ff-only && mkdir -p /home/nwm/tmp && NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=<writer url> TMPDIR=/home/nwm/tmp uv run pytest -q tests/test_mvt_national_identity_probe_integration.py <sibling integration file>` (throwaway database; no production DDL).
