# Re-ingesting a previously bootstrapped basin

When a basin was first seeded via `nhms-model bootstrap-qhh-production` (or any
other custom output-row writer), its `core.river_segment` `shud_output_river=true`
output rows carry `properties_json` shaped by that bootstrap, and its
`core.mesh_version` / `core.model_instance` rows reference the bootstrap's
`package_version`. A plain `reingest-basin` against such a basin will trip
`BASINS_REGISTRY_CHECKSUM_CONFLICT` on `output_river_segment` and (after
2026-06-19 fix) the parent-version refresh helper will reconcile
`mesh_version` + `model_instance` — but the generic `_ensure_output_river_segments`
contract still cannot match the custom output-row `properties_json`.

Two CLI flags exist to skip that generic output-row work and let re-ingest
reuse the existing bootstrap-written output rows:

| flag | what it disables | when to use |
|---|---|---|
| `--no-seed-output-river` | the call to `_ensure_output_river_segments` | the basin already has `shud_output_river=true` rows from a custom bootstrap whose `properties_json` will never match the generic digest |
| `--no-backfill-output-geometry` | the post-import call to `_backfill_output_segment_geometry` | the existing output rows already carry their display geometry (the bootstrap stitched it on at first seed) |

Pair them: skipping the seed without also skipping the backfill leaves the
backfill running against output rows that the generic path never wrote, which
is a confusing no-op at best.

## When to use

- `basins_qhh_shud` — originally seeded via `nhms-model bootstrap-qhh-production`.
- `basins_heihe_shud` — originally seeded via a heihe-specific bootstrap path
  (same shape as qhh).
- Any future basin that owns a custom output-row seeding contract (use both
  flags whenever the canonical seed path is not the writer of the existing
  rows).

For all other basins (the 9 generic basins re-ingested cleanly in PR #570's
phase 1 — hetianhe / keliya / qinyijiang / weiganhe / xinanjiang_upstream /
zhaochen × 4), leave the flags unset; the generic seed + backfill is correct
and is what the spec requires.

## Example: re-ingest qhh on node-27

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM

DATABASE_URL=postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms \
NHMS_BASINS_ROOT=/home/ghdc/nwm/Basins \
OBJECT_STORE_ROOT=/home/nwm/NWM/.nhms-work/pr570/object-store \
OBJECT_STORE_PREFIX=s3://nhms \
uv run python -m workers.model_registry.cli reingest-basin \
  --basin-slug qhh \
  --model-id basins_qhh_shud \
  --package-version vbasins-reingest-$(date +%Y%m%d_%H%M%S) \
  --work-dir .nhms-work/qhh-reingest \
  --output artifacts/qhh-reingest.json \
  --auth-actor-id cli-model-admin \
  --auth-role model_admin \
  --no-seed-output-river \
  --no-backfill-output-geometry
```

The aggregate variant (`scripts/reingest_all_basins_receipt.py`) accepts the
same flags. They apply to every basin in the run, so use them only when
re-ingesting a single basin or a homogeneous group of custom-bootstrapped
basins.

## What still gets refreshed

`_refresh_parent_version_materialization` (PR #575) updates every parent row in
place when `_delete_legacy_seg_rows` reports it purged legacy rows:

- `core.river_network_version` — `segment_count`, `source_uri`, `checksum`
- `core.basin_version` — `source_uri`, `checksum`
- `core.mesh_version` — `mesh_uri`, `checksum`, `properties_json` (`package_checksum` + manifest references)
- `core.model_instance` — `model_package_uri`, `resource_profile`

That covers every CHECKSUM_CONFLICT path the parent metadata can trigger. The
output-row `properties_json` digest is the one remaining contract the flags
above are designed to skip.

## Data invariants to verify post-re-ingest

- `hydro.river_timeseries` row count for the basin's `river_network_version_id`
  is unchanged (every row in that table targets `<model>_shud_riv_*` output ids,
  not `<model>_seg_*` legacy reach ids, so the seg-row purge does not cascade).
- `core.river_segment` for the basin now holds only `<model>_reach_<iRiv:06d>`
  and `<model>_shud_riv_<iRiv:06d>` ids — zero legacy `<model>_seg_*` rows.
- `geom_null_count == 0` and `multi_part_violation_count == 0` on the reach
  rows (the PR-2 contract).
- `core.river_segment_crosswalk` row count equals the `gis/seg.shp` record
  count.

Spot-check on node-27:

```bash
docker exec nhms-db psql -U nhms -d nhms -c "
  SELECT mi.model_id, split_part(rs.river_segment_id, '_', 4) AS id_class, COUNT(*)
  FROM core.river_segment rs
  JOIN core.model_instance mi USING (river_network_version_id)
  WHERE mi.model_id = 'basins_qhh_shud'
  GROUP BY 1, 2 ORDER BY 1, 2;"
```
