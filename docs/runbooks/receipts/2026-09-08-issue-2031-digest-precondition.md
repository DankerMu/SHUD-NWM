# Receipt — issue #2031 precondition measurement (hydro-national digest vs tile SQL)

Read-only measurement on node-27's active primary, run before any design ruling (issue #2031
acceptance criterion 1). Nothing was written; the role has no INSERT/UPDATE/DELETE.

- Measured at: `2026-09-08 11:01:36 UTC` (server `now()`), role `nhms_display_ro`
- Target: node-27 `nhms-db` (PostgreSQL 15.2), via `infra/env/display.env` `DATABASE_URL`
- node-27 checkout at measurement time: `5a86841c` (the measurement reads production tables, not the checkout)
- SQL: `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f issue2031_measure.sql` (full script below)

## Conclusions

| Question | Result | Reading |
|---|---|---|
| A / new route: `(source, cycle, river_network_version_id)` groups with ≥2 display-ready, `segment_count > 0` runs (Q1) | **20 groups** (4839 have exactly 1) — all on `basins_lh_ylj_rivnet_vbasins`, gfs + ifs, cycles 2026-08-20 00Z … 2026-08-24 12Z | precondition **structurally reachable** |
| A / new route: among those 20, does the rank-1 run (`cycle_time DESC, run_id DESC`) fail to cover an instant a lower-ranked run covers? (Q1c) | **0 of 20** — the two runs in every group carry byte-identical coverage windows | **not diverging today**; the trigger needs the windows to differ |
| Origin of the double runs (follow-up query) | two `hydro_run` rows per cycle with different `model_id` (`dg_c1b82d…` / `dg_945b6f…` for gfs, `dg_7a168f…` / `dg_6396d7…` for ifs), all on `basin_version_id = basins_lh_ylj_vbasins`; those `dg_*` model_instances are `active_flag = f`, the active instance is `basins_lh_ylj_shud` | candidates join through `mi.basin_version_id = h.basin_version_id`, so runs of a **retired direct-grid variant** still rank for the active network — this is the reachable shape, not a re-run |
| A / legacy source-less route: networks whose OVERALL-latest run's window excludes instants an older run covers (Q3) | **38 of 38** networks | legacy divergence **reachable now** for every `valid_time` earlier than the latest cycle's window start |
| `model_instance` multiplicity: active instances per `(basin_version_id, river_network_version_id)` (Q2) | 38 pairs, all `n = 1` | this multiplicity source **does not exist** on node-27 |
| `hydro_run` uniqueness on `(model_id, source_id, cycle_time)` (Q4) | no unique index; data is unique today (7189 groups, all `n = 1`) | a re-run with a new `run_id` remains schema-legal |
| B: output-river rows a backfill would move today (Q5) | 38 networks, `geom_null = 0`, `type_missing = 0` everywhere | B is **latent** (fires on the next basin bootstrap / re-import), not active |
| B: spare column on `core.river_network_version` (Q5b) | 8 columns, none free (`checksum` is import-owned) | a geometry-generation signal needs a migration |
| C: `map.tile_cache` rows (Q6) | **0 rows** | the DB tile cache is unused on node-27 (`nhms_display_ro` cannot write it); the ~4.0 GB `NHMS_MVT_FILE_CACHE_DIR` file cache is the live cache layer (size from a separate `du -sh` on the display.env cache dir in the same session — not part of the SQL script below; #2032's acceptance still owes the full `du`/`.pbf`/`.locks`/`df` set) |

Ruling taken from these numbers is recorded in
`openspec/changes/fix-national-digest-cache-identity-2031/design.md` (D1–D8) and in the appendix of
`openspec/changes/display-v2-national-timeline-precip-overlay/invariant-matrix-i4-2007.md`.

## Script

```sql
\set ON_ERROR_STOP on
\timing off
\pset footer on
SELECT now() AS measured_at, current_user, version();

-- Q1: display-ready, river-bearing run candidates per (source, cycle, network).
--     This is exactly the digest's candidate set (no valid_time predicate).
\echo '=== Q1 distribution of candidate-run count per (source_id, cycle_time, river_network_version_id) ==='
WITH cand AS (
  SELECT lower(h.source_id) AS source_id, h.cycle_time, mi.river_network_version_id,
         h.run_id, h.status, rdc.river_valid_time_start, rdc.river_valid_time_end, rdc.segment_count
  FROM hydro.hydro_run h
  JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
  JOIN hydro.run_display_coverage rdc ON rdc.run_id = h.run_id AND rdc.segment_count > 0
  WHERE h.status IN ('succeeded','parsed','published')
    AND mi.river_network_version_id IS NOT NULL AND mi.active_flag
)
SELECT n_runs, COUNT(*) AS identity_groups
FROM (SELECT source_id, cycle_time, river_network_version_id, COUNT(*) AS n_runs FROM cand GROUP BY 1,2,3) g
GROUP BY n_runs ORDER BY n_runs;

\echo '=== Q1b groups with >=2 candidate runs, with their coverage windows (limit 40) ==='
WITH cand AS (
  SELECT lower(h.source_id) AS source_id, h.cycle_time, mi.river_network_version_id,
         h.run_id, h.status, rdc.river_valid_time_start, rdc.river_valid_time_end
  FROM hydro.hydro_run h
  JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
  JOIN hydro.run_display_coverage rdc ON rdc.run_id = h.run_id AND rdc.segment_count > 0
  WHERE h.status IN ('succeeded','parsed','published')
    AND mi.river_network_version_id IS NOT NULL AND mi.active_flag
), multi AS (
  SELECT source_id, cycle_time, river_network_version_id FROM cand GROUP BY 1,2,3 HAVING COUNT(*) >= 2
)
SELECT c.source_id, c.cycle_time, c.river_network_version_id, c.run_id, c.status,
       c.river_valid_time_start, c.river_valid_time_end
FROM cand c JOIN multi m USING (source_id, cycle_time, river_network_version_id)
ORDER BY 1,2,3, c.run_id DESC LIMIT 40;

\echo '=== Q1c among >=2 groups: does the rank-1 run (cycle_time DESC, run_id DESC) fail to cover an instant a lower-ranked run covers? ==='
WITH cand AS (
  SELECT lower(h.source_id) AS source_id, h.cycle_time, mi.river_network_version_id,
         h.run_id, rdc.river_valid_time_start AS s, rdc.river_valid_time_end AS e,
         ROW_NUMBER() OVER (PARTITION BY lower(h.source_id), h.cycle_time, mi.river_network_version_id
                            ORDER BY h.cycle_time DESC, h.run_id DESC) AS rn
  FROM hydro.hydro_run h
  JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
  JOIN hydro.run_display_coverage rdc ON rdc.run_id = h.run_id AND rdc.segment_count > 0
  WHERE h.status IN ('succeeded','parsed','published')
    AND mi.river_network_version_id IS NOT NULL AND mi.active_flag
)
SELECT COUNT(*) FILTER (WHERE diverging) AS diverging_groups, COUNT(*) AS multi_groups
FROM (
  SELECT top.source_id, top.cycle_time, top.river_network_version_id,
         bool_or(o.s < top.s OR o.e > top.e) AS diverging
  FROM cand top JOIN cand o USING (source_id, cycle_time, river_network_version_id)
  WHERE top.rn = 1 AND o.rn > 1
  GROUP BY 1,2,3
) d;

-- Q2: model_instance multiplicity: same basin_version_id, >1 active instance sharing a network.
\echo '=== Q2 active model_instance rows per (basin_version_id, river_network_version_id) ==='
SELECT n, COUNT(*) AS pairs FROM (
  SELECT basin_version_id, river_network_version_id, COUNT(*) AS n
  FROM core.model_instance WHERE active_flag AND river_network_version_id IS NOT NULL
  GROUP BY 1,2
) t GROUP BY n ORDER BY n;

\echo '=== Q2b active instances per basin_version_id (any network) ==='
SELECT n, COUNT(*) AS basins FROM (
  SELECT basin_version_id, COUNT(*) AS n FROM core.model_instance WHERE active_flag GROUP BY 1
) t GROUP BY n ORDER BY n;

-- Q3: legacy source-less route: per network, does the OVERALL-latest run's window fail to
--     cover an instant that an OLDER display-ready run covers? (digest ranks latest; tile at
--     that instant falls back to the older run)
\echo '=== Q3 legacy route: networks whose overall-latest run window excludes instants older runs cover ==='
WITH cand AS (
  SELECT mi.river_network_version_id, h.run_id, h.cycle_time,
         rdc.river_valid_time_start AS s, rdc.river_valid_time_end AS e,
         ROW_NUMBER() OVER (PARTITION BY mi.river_network_version_id ORDER BY h.cycle_time DESC, h.run_id DESC) AS rn
  FROM hydro.hydro_run h
  JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
  JOIN hydro.run_display_coverage rdc ON rdc.run_id = h.run_id AND rdc.segment_count > 0
  WHERE h.status IN ('succeeded','parsed','published')
    AND mi.river_network_version_id IS NOT NULL AND mi.active_flag
)
SELECT COUNT(*) AS networks,
       COUNT(*) FILTER (WHERE older_covers_outside_latest) AS networks_with_older_only_instants,
       COUNT(*) FILTER (WHERE n_runs >= 2) AS networks_with_multiple_runs
FROM (
  SELECT top.river_network_version_id,
         COUNT(o.run_id) + 1 AS n_runs,
         bool_or(o.s < top.s OR o.e > top.e) AS older_covers_outside_latest
  FROM cand top LEFT JOIN cand o ON o.river_network_version_id = top.river_network_version_id AND o.rn > 1
  WHERE top.rn = 1 GROUP BY 1
) t;

\echo '=== Q3b sample of latest-vs-older windows per network (limit 12) ==='
WITH cand AS (
  SELECT mi.river_network_version_id, h.run_id, lower(h.source_id) AS source_id, h.cycle_time,
         rdc.river_valid_time_start AS s, rdc.river_valid_time_end AS e,
         ROW_NUMBER() OVER (PARTITION BY mi.river_network_version_id ORDER BY h.cycle_time DESC, h.run_id DESC) AS rn
  FROM hydro.hydro_run h
  JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
  JOIN hydro.run_display_coverage rdc ON rdc.run_id = h.run_id AND rdc.segment_count > 0
  WHERE h.status IN ('succeeded','parsed','published')
    AND mi.river_network_version_id IS NOT NULL AND mi.active_flag
)
SELECT river_network_version_id, rn, source_id, cycle_time, s, e FROM cand WHERE rn <= 3 ORDER BY 1,2 LIMIT 12;

-- Q4: uniqueness on hydro_run (model_id, source_id, cycle_time)?
\echo '=== Q4 hydro_run indexes ==='
SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='hydro' AND tablename='hydro_run' ORDER BY 1;

\echo '=== Q4b hydro_run rows sharing (model_id, source_id, cycle_time), any status ==='
SELECT n, COUNT(*) AS groups FROM (
  SELECT model_id, lower(source_id) AS source_id, cycle_time, COUNT(*) AS n FROM hydro.hydro_run GROUP BY 1,2,3
) t GROUP BY n ORDER BY n;

-- Q5: B side — output-river rows still NULL geom / missing Type per network (what a backfill would move)
\echo '=== Q5 output-river rows per network: null geom / missing Type ==='
SELECT rs.river_network_version_id,
       COUNT(*) AS output_rows,
       COUNT(*) FILTER (WHERE rs.geom IS NULL) AS geom_null,
       COUNT(*) FILTER (WHERE NOT rs.properties_json ? 'Type') AS type_missing
FROM core.river_segment rs
JOIN core.model_instance mi ON mi.river_network_version_id = rs.river_network_version_id AND mi.active_flag
WHERE COALESCE(rs.properties_json->>'shud_output_river','false') = 'true'
GROUP BY 1 ORDER BY 1;

\echo '=== Q5b core.river_network_version columns ==='
SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns
WHERE table_schema='core' AND table_name='river_network_version' ORDER BY ordinal_position;

\echo '=== Q6 tile_cache size for hydro-national layers ==='
SELECT layer_id, COUNT(*) AS rows, MIN(created_at) AS oldest, MAX(created_at) AS newest
FROM map.tile_cache GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

## Output

```text
Timing is off.
         measured_at          |  current_user   |                                                              version                                                              
------------------------------+-----------------+-----------------------------------------------------------------------------------------------------------------------------------
 2026-09-08 11:01:36.45694+00 | nhms_display_ro | PostgreSQL 15.2 (Ubuntu 15.2-1.pgdg22.04+1) on x86_64-pc-linux-gnu, compiled by gcc (Ubuntu 11.3.0-1ubuntu1~22.04) 11.3.0, 64-bit
(1 row)

=== Q1 distribution of candidate-run count per (source_id, cycle_time, river_network_version_id) ===
 n_runs | identity_groups 
--------+-----------------
      1 |            4839
      2 |              20
(2 rows)

=== Q1b groups with >=2 candidate runs, with their coverage windows (limit 40) ===
 source_id |       cycle_time       |   river_network_version_id   |                         run_id                          |  status   | river_valid_time_start |  river_valid_time_end  
-----------+------------------------+------------------------------+---------------------------------------------------------+-----------+------------------------+------------------------
 gfs       | 2026-08-20 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082000_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-20 00:00:00+00 | 2026-08-26 21:00:00+00
 gfs       | 2026-08-20 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082000_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-20 00:00:00+00 | 2026-08-26 21:00:00+00
 gfs       | 2026-08-20 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082012_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-20 12:00:00+00 | 2026-08-27 09:00:00+00
 gfs       | 2026-08-20 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082012_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-20 12:00:00+00 | 2026-08-27 09:00:00+00
 gfs       | 2026-08-21 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082100_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-21 00:00:00+00 | 2026-08-27 21:00:00+00
 gfs       | 2026-08-21 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082100_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-21 00:00:00+00 | 2026-08-27 21:00:00+00
 gfs       | 2026-08-21 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082112_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-21 12:00:00+00 | 2026-08-28 09:00:00+00
 gfs       | 2026-08-21 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082112_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-21 12:00:00+00 | 2026-08-28 09:00:00+00
 gfs       | 2026-08-22 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082200_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-22 00:00:00+00 | 2026-08-28 21:00:00+00
 gfs       | 2026-08-22 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082200_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-22 00:00:00+00 | 2026-08-28 21:00:00+00
 gfs       | 2026-08-22 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082212_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-22 12:00:00+00 | 2026-08-29 09:00:00+00
 gfs       | 2026-08-22 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082212_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-22 12:00:00+00 | 2026-08-29 09:00:00+00
 gfs       | 2026-08-23 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082300_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-23 00:00:00+00 | 2026-08-29 21:00:00+00
 gfs       | 2026-08-23 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082300_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-23 00:00:00+00 | 2026-08-29 21:00:00+00
 gfs       | 2026-08-23 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082312_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-23 12:00:00+00 | 2026-08-30 09:00:00+00
 gfs       | 2026-08-23 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082312_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-23 12:00:00+00 | 2026-08-30 09:00:00+00
 gfs       | 2026-08-24 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082400_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-24 00:00:00+00 | 2026-08-30 21:00:00+00
 gfs       | 2026-08-24 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082400_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-24 00:00:00+00 | 2026-08-30 21:00:00+00
 gfs       | 2026-08-24 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082412_dg_c1b82d581806931d405e8fd11fb24719 | published | 2026-08-24 12:00:00+00 | 2026-08-31 09:00:00+00
 gfs       | 2026-08-24 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_gfs_2026082412_dg_945b6f0bbf63c5314d47df9481892229 | published | 2026-08-24 12:00:00+00 | 2026-08-31 09:00:00+00
 ifs       | 2026-08-20 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082000_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-20 00:00:00+00 | 2026-08-26 23:00:00+00
 ifs       | 2026-08-20 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082000_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-20 00:00:00+00 | 2026-08-26 23:00:00+00
 ifs       | 2026-08-20 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082012_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-20 12:00:00+00 | 2026-08-27 11:00:00+00
 ifs       | 2026-08-20 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082012_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-20 12:00:00+00 | 2026-08-27 11:00:00+00
 ifs       | 2026-08-21 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082100_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-21 00:00:00+00 | 2026-08-27 23:00:00+00
 ifs       | 2026-08-21 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082100_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-21 00:00:00+00 | 2026-08-27 23:00:00+00
 ifs       | 2026-08-21 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082112_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-21 12:00:00+00 | 2026-08-28 11:00:00+00
 ifs       | 2026-08-21 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082112_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-21 12:00:00+00 | 2026-08-28 11:00:00+00
 ifs       | 2026-08-22 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082200_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-22 00:00:00+00 | 2026-08-28 23:00:00+00
 ifs       | 2026-08-22 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082200_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-22 00:00:00+00 | 2026-08-28 23:00:00+00
 ifs       | 2026-08-22 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082212_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-22 12:00:00+00 | 2026-08-29 11:00:00+00
 ifs       | 2026-08-22 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082212_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-22 12:00:00+00 | 2026-08-29 11:00:00+00
 ifs       | 2026-08-23 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082300_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-23 00:00:00+00 | 2026-08-29 23:00:00+00
 ifs       | 2026-08-23 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082300_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-23 00:00:00+00 | 2026-08-29 23:00:00+00
 ifs       | 2026-08-23 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082312_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-23 12:00:00+00 | 2026-08-30 11:00:00+00
 ifs       | 2026-08-23 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082312_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-23 12:00:00+00 | 2026-08-30 11:00:00+00
 ifs       | 2026-08-24 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082400_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-24 00:00:00+00 | 2026-08-30 23:00:00+00
 ifs       | 2026-08-24 00:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082400_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-24 00:00:00+00 | 2026-08-30 23:00:00+00
 ifs       | 2026-08-24 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082412_dg_7a168f6fca7dc9209180a0c4a6bfc68e | published | 2026-08-24 12:00:00+00 | 2026-08-31 11:00:00+00
 ifs       | 2026-08-24 12:00:00+00 | basins_lh_ylj_rivnet_vbasins | fcst_ifs_2026082412_dg_6396d77756e693a12277c682f6cd22ee | published | 2026-08-24 12:00:00+00 | 2026-08-31 11:00:00+00
(40 rows)

=== Q1c among >=2 groups: does the rank-1 run (cycle_time DESC, run_id DESC) fail to cover an instant a lower-ranked run covers? ===
 diverging_groups | multi_groups 
------------------+--------------
                0 |           20
(1 row)

=== Q2 active model_instance rows per (basin_version_id, river_network_version_id) ===
 n | pairs 
---+-------
 1 |    38
(1 row)

=== Q2b active instances per basin_version_id (any network) ===
 n | basins 
---+--------
 1 |     38
(1 row)

=== Q3 legacy route: networks whose overall-latest run window excludes instants older runs cover ===
 networks | networks_with_older_only_instants | networks_with_multiple_runs 
----------+-----------------------------------+-----------------------------
       38 |                                38 |                          38
(1 row)

=== Q3b sample of latest-vs-older windows per network (limit 12) ===
   river_network_version_id   | rn | source_id |       cycle_time       |           s            |           e            
------------------------------+----+-----------+------------------------+------------------------+------------------------
 basins_byh_rivnet_vbasins    |  1 | ifs       | 2026-09-07 12:00:00+00 | 2026-09-07 12:00:00+00 | 2026-09-14 11:00:00+00
 basins_byh_rivnet_vbasins    |  2 | gfs       | 2026-09-07 12:00:00+00 | 2026-09-07 12:00:00+00 | 2026-09-14 09:00:00+00
 basins_byh_rivnet_vbasins    |  3 | ifs       | 2026-09-07 00:00:00+00 | 2026-09-07 00:00:00+00 | 2026-09-13 23:00:00+00
 basins_dth_ls_rivnet_vbasins |  1 | ifs       | 2026-09-07 12:00:00+00 | 2026-09-07 12:00:00+00 | 2026-09-14 11:00:00+00
 basins_dth_ls_rivnet_vbasins |  2 | gfs       | 2026-09-07 12:00:00+00 | 2026-09-07 12:00:00+00 | 2026-09-14 09:00:00+00
 basins_dth_ls_rivnet_vbasins |  3 | ifs       | 2026-09-07 00:00:00+00 | 2026-09-07 00:00:00+00 | 2026-09-13 23:00:00+00
 basins_dth_xj_rivnet_vbasins |  1 | ifs       | 2026-09-07 12:00:00+00 | 2026-09-07 12:00:00+00 | 2026-09-14 11:00:00+00
 basins_dth_xj_rivnet_vbasins |  2 | gfs       | 2026-09-07 12:00:00+00 | 2026-09-07 12:00:00+00 | 2026-09-14 09:00:00+00
 basins_dth_xj_rivnet_vbasins |  3 | ifs       | 2026-09-07 00:00:00+00 | 2026-09-07 00:00:00+00 | 2026-09-13 23:00:00+00
 basins_dth_yj_rivnet_vbasins |  1 | ifs       | 2026-09-07 12:00:00+00 | 2026-09-07 12:00:00+00 | 2026-09-14 11:00:00+00
 basins_dth_yj_rivnet_vbasins |  2 | gfs       | 2026-09-07 12:00:00+00 | 2026-09-07 12:00:00+00 | 2026-09-14 09:00:00+00
 basins_dth_yj_rivnet_vbasins |  3 | ifs       | 2026-09-07 00:00:00+00 | 2026-09-07 00:00:00+00 | 2026-09-13 23:00:00+00
(12 rows)

=== Q4 hydro_run indexes ===
                  indexname                   |                                                                                                                                                                         indexdef                                                                                                                                                                         
----------------------------------------------+----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 hydro_run_display_product_basin_status_idx   | CREATE INDEX hydro_run_display_product_basin_status_idx ON hydro.hydro_run USING btree (basin_version_id, status) WHERE (status = ANY (ARRAY['parsed'::hydro.run_status, 'frequency_done'::hydro.run_status, 'published'::hydro.run_status]))
 hydro_run_display_ready_basin_status_idx     | CREATE INDEX hydro_run_display_ready_basin_status_idx ON hydro.hydro_run USING btree (basin_version_id, status) WHERE (status = ANY (ARRAY['succeeded'::hydro.run_status, 'parsed'::hydro.run_status, 'frequency_done'::hydro.run_status, 'published'::hydro.run_status]))
 hydro_run_display_ready_candidate_idx        | CREATE INDEX hydro_run_display_ready_candidate_idx ON hydro.hydro_run USING btree (lower(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC) WHERE ((cycle_time IS NOT NULL) AND (status = ANY (ARRAY['succeeded'::hydro.run_status, 'parsed'::hydro.run_status, 'frequency_done'::hydro.run_status, 'published'::hydro.run_status])))
 hydro_run_latest_ready_run_idx               | CREATE INDEX hydro_run_latest_ready_run_idx ON hydro.hydro_run USING btree (cycle_time DESC, run_id DESC) WHERE (status = ANY (ARRAY['frequency_done'::hydro.run_status, 'published'::hydro.run_status]))
 hydro_run_ops_strict_identity_candidates_idx | CREATE INDEX hydro_run_ops_strict_identity_candidates_idx ON hydro.hydro_run USING btree (source_id, cycle_time, run_id, model_id)
 hydro_run_pkey                               | CREATE UNIQUE INDEX hydro_run_pkey ON hydro.hydro_run USING btree (run_id)
 hydro_run_qhh_latest_candidate_idx           | CREATE INDEX hydro_run_qhh_latest_candidate_idx ON hydro.hydro_run USING btree (lower(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC) WHERE ((cycle_time IS NOT NULL) AND (status = ANY (ARRAY['frequency_done'::hydro.run_status, 'published'::hydro.run_status])))
 hydro_run_qhh_latest_candidate_parsed_idx    | CREATE INDEX hydro_run_qhh_latest_candidate_parsed_idx ON hydro.hydro_run USING btree (lower(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC) WHERE ((cycle_time IS NOT NULL) AND (status = ANY (ARRAY['parsed'::hydro.run_status, 'frequency_done'::hydro.run_status, 'published'::hydro.run_status])))
 hydro_run_run_key_key                        | CREATE UNIQUE INDEX hydro_run_run_key_key ON hydro.hydro_run USING btree (run_key)
(9 rows)

=== Q4b hydro_run rows sharing (model_id, source_id, cycle_time), any status ===
 n | groups 
---+--------
 1 |   7189
(1 row)

=== Q5 output-river rows per network: null geom / missing Type ===
            river_network_version_id            | output_rows | geom_null | type_missing 
------------------------------------------------+-------------+-----------+--------------
 basins_byh_rivnet_vbasins                      |        7971 |         0 |            0
 basins_dth_ls_rivnet_vbasins                   |        1654 |         0 |            0
 basins_dth_xj_rivnet_vbasins                   |        8829 |         0 |            0
 basins_dth_yj_rivnet_vbasins                   |        8622 |         0 |            0
 basins_dth_zj_rivnet_vbasins                   |        2474 |         0 |            0
 basins_haihe_daqinghe_rivnet_vbasins           |        5377 |         0 |            0
 basins_haihe_luanhe_rivnet_vbasins             |        5856 |         0 |            0
 basins_haihe_yongdinghe_rivnet_vbasins         |        8890 |         0 |            0
 basins_haihe_zhangweihe_rivnet_vbasins         |        8423 |         0 |            0
 basins_haihe_ziyahe_rivnet_vbasins             |        8275 |         0 |            0
 basins_heihe_rivnet_vbasins                    |        2352 |         0 |            0
 basins_hekouzhen_zhi_longmen_rivnet_vbasins    |        7644 |         0 |            0
 basins_hetianhe_rivnet_vbasins                 |        1858 |         0 |            0
 basins_hj_rivnet_vbasins                       |        7955 |         0 |            0
 basins_huai_main_rivnet_vbasins                |        9454 |         0 |            0
 basins_huaiyss_rivnet_vbasins                  |        4469 |         0 |            0
 basins_jialingjiang_rivnet_vbasins             |       14673 |         0 |            0
 basins_kashigeer_rivnet_vbasins                |        2456 |         0 |            0
 basins_keliya_rivnet_vbasins                   |         333 |         0 |            0
 basins_lanzhou_zhi_hekouzhen_rivnet_vbasins    |       12824 |         0 |            0
 basins_lh_gl_rivnet_vbasins                    |       12409 |         0 |            0
 basins_lh_ldbd_rivnet_vbasins                  |        1040 |         0 |            0
 basins_lh_lxyh_rivnet_vbasins                  |        1681 |         0 |            0
 basins_lh_ylj_rivnet_vbasins                   |        3049 |         0 |            0
 basins_longmen_zhi_sanmenxia_rivnet_vbasins    |       13323 |         0 |            0
 basins_longyangxia_yishang_rivnet_vbasins      |        9187 |         0 |            0
 basins_longyangxia_zhi_lanzhou_rivnet_vbasins  |        6087 |         0 |            0
 basins_mj_rivnet_vbasins                       |        7437 |         0 |            0
 basins_qhh_rivnet_vbasins                      |        1633 |         0 |            0
 basins_qinyijiang_rivnet_vbasins               |         319 |         0 |            0
 basins_sanmenxia_zhi_huayuankou_rivnet_vbasins |        2969 |         0 |            0
 basins_shj_2shj_rivnet_vbasins                 |        3770 |         0 |            0
 basins_shj_3shj_rivnet_vbasins                 |       10079 |         0 |            0
 basins_shj_nj_rivnet_vbasins                   |       16009 |         0 |            0
 basins_tailanhe_rivnet_vbasins                 |          63 |         0 |            0
 basins_weiganhe_rivnet_vbasins                 |        1379 |         0 |            0
 basins_wj_rivnet_vbasins                       |        4012 |         0 |            0
 basins_xinanjiang_upstream_rivnet_vbasins      |         216 |         0 |            0
(38 rows)

=== Q5b core.river_network_version columns ===
        column_name        |        data_type         | is_nullable | column_default 
---------------------------+--------------------------+-------------+----------------
 river_network_version_id  | text                     | NO          | 
 basin_version_id          | text                     | NO          | 
 version_label             | text                     | NO          | 
 segment_count             | integer                  | NO          | 
 source_uri                | text                     | YES         | 
 checksum                  | text                     | YES         | 
 created_at                | timestamp with time zone | NO          | now()
 river_network_version_key | integer                  | NO          | 
(8 rows)

=== Q6 tile_cache size for hydro-national layers ===
 layer_id | rows | oldest | newest 
----------+------+--------+--------
(0 rows)

```

## Follow-up query (double-run origin)

```sql
SELECT h.run_id, h.model_id, h.basin_version_id, h.status, h.run_type, h.created_at
FROM hydro.hydro_run h
WHERE h.cycle_time = TIMESTAMPTZ '2026-08-24 12:00:00+00' AND lower(h.source_id) = 'gfs'
  AND h.basin_version_id IN (SELECT basin_version_id FROM core.model_instance
                             WHERE river_network_version_id = 'basins_lh_ylj_rivnet_vbasins')
ORDER BY run_id;
SELECT model_id, basin_version_id, river_network_version_id, active_flag
FROM core.model_instance WHERE river_network_version_id = 'basins_lh_ylj_rivnet_vbasins';
```

```text
                         run_id                          |              model_id               |   basin_version_id    |  status   | run_type |          created_at
---------------------------------------------------------+-------------------------------------+-----------------------+-----------+----------+-------------------------------
 fcst_gfs_2026082412_dg_945b6f0bbf63c5314d47df9481892229 | dg_945b6f0bbf63c5314d47df9481892229 | basins_lh_ylj_vbasins | published | forecast | 2026-08-26 11:47:12.471364+00
 fcst_gfs_2026082412_dg_c1b82d581806931d405e8fd11fb24719 | dg_c1b82d581806931d405e8fd11fb24719 | basins_lh_ylj_vbasins | published | forecast | 2026-08-28 14:16:47.416112+00
(2 rows)

              model_id               |   basin_version_id    |   river_network_version_id   | active_flag
-------------------------------------+-----------------------+------------------------------+-------------
 dg_945b6f0bbf63c5314d47df9481892229 | basins_lh_ylj_vbasins | basins_lh_ylj_rivnet_vbasins | f
 dg_6396d77756e693a12277c682f6cd22ee | basins_lh_ylj_vbasins | basins_lh_ylj_rivnet_vbasins | f
 basins_lh_ylj_shud                  | basins_lh_ylj_vbasins | basins_lh_ylj_rivnet_vbasins | t
 dg_c1b82d581806931d405e8fd11fb24719 | basins_lh_ylj_vbasins | basins_lh_ylj_rivnet_vbasins | f
 dg_7a168f6fca7dc9209180a0c4a6bfc68e | basins_lh_ylj_vbasins | basins_lh_ylj_rivnet_vbasins | f
(5 rows)
```

Migration ledger at measurement time: `public.schema_migrations` holds 56 rows (through
`000056_hydro_run_parsed_at.sql`); `hydro.hydro_run.parsed_at` present. The `000057` column this
change adds is therefore **not** on node-27 yet — deploy order is migration first, display API second
(see design.md D6).
