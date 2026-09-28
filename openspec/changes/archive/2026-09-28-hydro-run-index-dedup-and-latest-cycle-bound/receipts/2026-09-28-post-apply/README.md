# 000065 production apply receipt (node-27, 2026-09-28)

User go-ahead: 2026-09-28 ("现在执行窗口"). Window script `window-000065.sh`, log `window-000065.log`.

| step | time (UTC) | result |
|---|---|---|
| 1 stop `nhms-node27-autopipe.timer` / `nhms-node27-download.timer` | 23:08:35 | both services inactive, nothing in flight |
| 2 `git status --porcelain` empty, `git pull --ff-only` | 23:08:35 | `63d23212` → `ba8f3b64` (merge of PR #2688) |
| 3 `.venv/bin/python -m packages.common.migrate` (`nhms` DSN) | 23:08:39 | attempt 1 rc 0: `Applied migration: 000065_hydro_run_candidate_index_dedup.sql`, 58 skipped, no lock timeout |
| 4 restart timers | 23:08:39 | both `active` |

Display API was not restarted (the new D1 SQL takes effect at its next restart).

## pg_indexes (`post-apply-pg_indexes.out`, read-only `nhms_display_ro`)

- 7 indexes on `hydro.hydro_run`, all `indisvalid` / `indisready`.
- `hydro_run_forecast_basin_cycle_idx`: `CREATE INDEX … USING btree (basin_version_id, cycle_time DESC) WHERE ((run_type = 'forecast'::hydro.run_type) AND (cycle_time IS NOT NULL))` — matches 000065.
- `hydro_run_qhh_latest_candidate_parsed_idx`, `hydro_run_display_ready_candidate_idx`, `hydro_run_display_ready_basin_status_idx`: absent.
- Ledger max `000065_hydro_run_candidate_index_dedup.sql`.

## D1 latest-cycle discovery (#2626 acceptance)

Real code path (`/home/nwm/tmp/l2/probes/explain_d1.py`, WT = `/home/nwm/NWM` at `ba8f3b64`, `post-apply-d1-byh.jsonl`), pin `basins_byh` reach 000001, warm ×3:

| scenarios | shared hit (post-apply) | pre-apply same day (master) | L2 baseline | result |
|---|---|---|---|---|
| IFS | 131 | 1379 | 1386 | `forecast_ifs_deterministic` 2026-09-28 00Z |
| GFS+IFS | 136 | 1450 | 1464 | GFS and IFS 2026-09-28 00Z |

`post-apply-d1-explain-byh-lat.out` (same statement shape): `cand` is `Bitmap Index Scan on hydro_run_forecast_basin_cycle_idx (rows=130)`, no `Seq Scan on hydro_run`; total 166 shared hit.

## Sibling selectors (#2634 acceptance, `post-apply-sibling-explain.out`)

Production read-only, SQL taken from the code:
- `display_ready_run` → `hydro_run_latest_ready_run_idx`
- QHH latest-product candidate CTE and fast path → `hydro_run_qhh_latest_candidate_idx`
- display-coverage candidate CTE → Bitmap on `hydro_run_display_product_basin_status_idx`
- `_eligible_run_ids` → `hydro_run_latest_ready_run_idx`
- The publish UPDATE cannot be EXPLAINed by the read-only role; its plan on the 000065 scratch copy is in `../2026-09-28-branch/sibling-explain-nhms_scratch_batchdb_34*.out` (`hydro_run_display_product_basin_status_idx`).

## F1 re-measure (`post-apply-f1-*.out`, `f1_post_apply.sh`)

Missing-row candidates on the newest windows, root shared hit (3rd warm run):

| basin | 00Z (2 missing) | 12Z (2 missing) | 00Z+12Z (4 missing) | pre-apply same shape |
|---|---|---|---|---|
| heihe | 1098 | 1095 | 2160 | 810 / 807 / 1584 |
| qhh | 1098 | 1095 | 2160 | 808 / 805 / 1580 |

The increase against the pre-apply receipt (hours earlier) is the hot sparse chunks filling (`Rows Removed by Filter` 264/168/72 vs 216/120/24 on chunks 231/233/237), which is the design D3 F1 mechanism: ≈ rows of the segment in the window on chunks where the planner picks `river_ts_segment_time_key_idx`. With `cand` now ≈ 30–40 hit, the whole statement in the 4-missing transient state is ≈ 2200, below D11 5000. The D3 stop rule stands (a receipt > 5000 reopens it).

## Service health

`GET /health` 200, `GET /api/v1/basins` 200 on `127.0.0.1:8080`; zero ` 500 ` lines in `nhms-display-api.service` journal since 23:08.
