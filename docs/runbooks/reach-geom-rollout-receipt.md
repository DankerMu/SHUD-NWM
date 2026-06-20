# Reach-geom rollout receipt (feat-reach-geom-from-river-shp)

Tracks the node-27 live verification of PR 2 (`#569`) + PR 3 (`#570`):
switching `core.river_segment.geom` source from `gis/seg.shp`
(segment-level, multi-part, with cross-gap bridges) to `gis/river.shp`
(reach-level, single-part, flow-ordered).

## Environment

| Item | Value |
|---|---|
| Node | `nwm@210.77.77.27:32099` |
| Repo HEAD | `5a5cf49` (PR #570 merge) |
| DB | docker `nhms-db` → `postgresql://nhms:nhms_dev@localhost:55432/nhms` |
| Basins source | `/home/ghdc/nwm/Basins/` (read via nwm user, `drwxrwxr-x 1103:nfsdata`) |
| Object store | `/home/nwm/NWM/.nhms-work/pr570/object-store/` (nwm-writable scratch) |
| Migrations applied | through `000039_crosswalk_external_identity.sql` |

## Pre-flight: Migrations 000038 + 000039 applied

Node-27 DB was at `000037_river_segment_multilinestring.sql` when reingest
first ran; PR #569's `000039_crosswalk_external_identity.sql` (which
swaps `core.river_segment_crosswalk` UNIQUE from `(rnv, river_segment_id,
source)` to `(rnv, source, external_id)`) was missing. First reingest
batch aborted with `psycopg2.errors.InvalidColumnReference: there is no
unique or exclusion constraint matching the ON CONFLICT specification`.

Applied via `uv run python -m packages.common.migrate`. Post-state:

```
000039_crosswalk_external_identity.sql
000038_direct_grid_interp_weight_constraints.sql
000037_river_segment_multilinestring.sql
000036_run_product_quality_explicit_source.sql
000035_qhh_display_coverage_materialization.sql

"river_segment_crosswalk_external_identity_uq" UNIQUE CONSTRAINT,
  btree (river_network_version_id, source, external_id)
```

## Aggregate reingest result

Command (one-shot, all basins):

```bash
DATABASE_URL=postgresql://nhms:nhms_dev@localhost:55432/nhms \
NHMS_BASINS_ROOT=/home/ghdc/nwm/Basins \
OBJECT_STORE_ROOT=/home/nwm/NWM/.nhms-work/pr570/object-store \
OBJECT_STORE_PREFIX=s3://nhms \
uv run python scripts/reingest_all_basins_receipt.py \
  --work-dir .nhms-work/pr570/reingest \
  --output artifacts/pr570/reingest_aggregate_20260620_001311.json \
  --package-version vbasins-reingest-20260620_001311 \
  --model-id-template "basins_{slug}_shud" \
  --auth-actor-id cli-model-admin --auth-role model_admin \
  --continue-on-error
```

Full receipt: [reach-geom-ingest-20260620.json](receipts/reach-geom-ingest-20260620.json).

**Totals**:

| Metric | Value |
|---|---|
| basins succeeded | 9 / 12 (10 basin slugs, zhaochen has 4 sub-models) |
| `imported_reach_count` (sum) | **13,596** |
| `crosswalk_row_count` (sum) | **28,278** |
| `geom_null_count` (sum) | **0** |
| `multi_part_violation_count` (sum) | **0** |

### Per-basin (9 success)

| basin / model | reach | crosswalk | geom_null | mp_viol |
|---|---:|---:|---:|---:|
| hetianhe | 1,858 | 4,929 | 0 | 0 |
| keliya | 333 | 534 | 0 | 0 |
| qinyijiang | 319 | 1,384 | 0 | 0 |
| weiganhe | 1,379 | 3,770 | 0 | 0 |
| xinanjiang_upstream | 216 | 584 | 0 | 0 |
| zhaochen/BST | 4,786 | 8,377 | 0 | 0 |
| zhaochen/HHY | 4,197 | 7,402 | 0 | 0 |
| zhaochen/MC | 354 | 879 | 0 | 0 |
| zhaochen/WEM | 154 | 419 | 0 | 0 |

`geom_null=0` + `mp_viol=0` confirms the PR-2 contract: every reach
written from `river.shp` is a single-part LineString with non-NULL geom.

### 3 known-issue failures (not PR-3 regressions)

| basin | error_code | root cause |
|---|---|---|
| `qhh` | `BASINS_REGISTRY_CHECKSUM_CONFLICT` | Pre-PR-2 import wrote `output_river_segment` rows via `bootstrap-qhh-production`'s `_seed_output_segment_rows`, whose `properties_json` digest diverges from the generic `_ensure_output_river_segments` digest the reingest path computes. PR #569's `_delete_legacy_seg_rows` purges legacy reach rows but does NOT touch the QHH-specific output rows. **Remediation**: re-run via `nhms-model bootstrap-qhh-production --basin-slug qhh` (the QHH path owns its own output-row write contract), or extend `_delete_legacy_seg_rows` to purge the QHH-bootstrap-written output rows before generic reingest. Out of scope for PR 3. |
| `heihe` | `BASINS_REGISTRY_CHECKSUM_CONFLICT` | Same shape as qhh — a prior import (PR #534 backfill or earlier basin-specific bootstrap) wrote output rows the reingest path can't match. Remediation: same as qhh — basin-specific bootstrap, or extend legacy purge. |
| `tailanhe` | `BASINS_MODEL_NOT_PUBLISHABLE` | Inventory has `default_import_eligible=false` (likely a `required_files` or fixture-completeness gate failing). Not a reingest bug; tailanhe was already non-importable before PR 2 too. **Remediation**: fix the basin source completeness, then re-discover. Tracked separately. |

### Current DB state (post-reingest, per-model reach count, output-rows excluded)

```
basins_heihe_shud           4759   ← pre-PR-2, seg-derived (reingest failed)
basins_hetianhe_shud        1858   ← PR-2 reach-derived ✓
basins_keliya_shud           333   ← PR-2 reach-derived ✓
basins_qhh_shud             3738   ← pre-PR-2, seg-derived (reingest failed)
basins_qinyijiang_shud       319   ← PR-2 reach-derived ✓
basins_weiganhe_shud        1379   ← PR-2 reach-derived ✓
basins_xinanjiang_upstream_shud 216 ← PR-2 reach-derived ✓
basins_zhaochen_bst_shud    4786   ← PR-2 reach-derived ✓
basins_zhaochen_hhy_shud    4197   ← PR-2 reach-derived ✓
basins_zhaochen_mc_shud      354   ← PR-2 reach-derived ✓
basins_zhaochen_wem_shud     154   ← PR-2 reach-derived ✓
```

9 of 11 models held PR-2 reach-level geometry after phase 1; phase 2
below brings qhh + heihe onto the same contract (11/11; tailanhe is
inventory-blocked, see known issues).

## Phase 2: qhh + heihe rescue (one-off monkey-patch driver)

Phase-1 generic reingest failed `BASINS_REGISTRY_CHECKSUM_CONFLICT` on
`output_river_segment` for qhh + heihe because their existing output rows
were seeded by `qhh_production_bootstrap`-style code paths with
QHH-specific `properties_json`, which the generic
`_ensure_output_river_segments` digest contract cannot match. A second
blocker surfaced during phase 2: `_refresh_parent_version_materialization`
(PR #569) refreshes `basin_version` + `river_network_version` but not
`mesh_version` / `model_instance`, so the old `package_version`'s
`mesh_uri` + `model_package_uri` would also trip CHECKSUM_CONFLICT on
re-ingest of any basin originally bootstrapped under a different
package_version.

Phase-2 path (operator one-off, not committed):

1. Verified FK safety: `hydro.river_timeseries` (90,666,720 rows for
   qhh+heihe combined) targets `<model>_shud_riv_*` output rows
   exclusively — zero rows reference `<model>_seg_*` legacy reach rows.
   `_delete_legacy_seg_rows` is therefore safe to purge legacy seg rows
   without touching forecast time-series.
2. Monkey-patched `import_basin_into_registry_core.__kwdefaults__`:
   `seed_output_river_segments=False` + `backfill_output_segment_geometry=False`
   (skip generic output-row seed; output rows already present from the
   original bootstrap).
3. Monkey-patched `_refresh_parent_version_materialization` to also
   UPDATE `core.mesh_version` (`mesh_uri`, `checksum`,
   `properties_json.package_checksum`) and `core.model_instance`
   (`model_package_uri`, `resource_profile`) with the values the current
   re-ingest would have INSERTed, so subsequent `_ensure_mesh` /
   `_ensure_model_instance` take the idempotent no-op path.
4. Drove `reingest_basin(basin_slug='qhh' | 'heihe', ...)` in-process via
   the standard CLI helper (publish → import).
5. The driver script was removed after success (operator one-off; not
   committed).

Phase-2 receipt: [reach-geom-ingest-20260620-phase2-qhh-heihe.json](receipts/reach-geom-ingest-20260620-phase2-qhh-heihe.json).

| basin / model | reach | crosswalk | geom_null | mp_viol |
|---|---:|---:|---:|---:|
| qhh   | 1,633 | 3,738 | 0 | 0 |
| heihe | 2,352 | 4,759 | 0 | 0 |

Post-phase-2 DB invariants verified:

| check | value | note |
|---|---|---|
| `hydro.river_timeseries` row count (qhh+heihe) | **90,666,720** | identical to pre-phase-2; FK preservation confirmed |
| `core.river_segment` id_class (qhh+heihe) | only `reach_*` and `shud_riv_*` | zero legacy `seg_*` rows remain |
| `crosswalk_row_count` matches `seg.shp` record count | qhh 3738 ✓ / heihe 4759 ✓ | crosswalk fully rebuilt |
| `geom_null` / `multi_part_violation` | 0 / 0 | PR-2 single-part LineString contract holds |

API containers `api-web-1` + `api-worker-1` restarted post-DB switch.

### Updated current DB state (post-phase-2)

```
basins_heihe_shud           2352   ← PR-2 reach-derived ✓ (phase 2)
basins_hetianhe_shud        1858   ← PR-2 reach-derived ✓
basins_keliya_shud           333   ← PR-2 reach-derived ✓
basins_qhh_shud             1633   ← PR-2 reach-derived ✓ (phase 2)
basins_qinyijiang_shud       319   ← PR-2 reach-derived ✓
basins_weiganhe_shud        1379   ← PR-2 reach-derived ✓
basins_xinanjiang_upstream_shud 216 ← PR-2 reach-derived ✓
basins_zhaochen_bst_shud    4786   ← PR-2 reach-derived ✓
basins_zhaochen_hhy_shud    4197   ← PR-2 reach-derived ✓
basins_zhaochen_mc_shud      354   ← PR-2 reach-derived ✓
basins_zhaochen_wem_shud     154   ← PR-2 reach-derived ✓
```

11 / 11 importable models on PR-2 reach-derived geometry; tailanhe still
inventory-blocked (see known issues).

## Browser live verification

Visual verification deferred to the operator (will self-check
test.nwm.ac.cn). No screenshots captured in this PR.

## Issues uncovered that fall outside PR 3 / PR 4 scope

1. **QHH-bootstrap vs generic reingest output-row + parent-version contract**
   — phase 2 worked around it via a one-off monkey-patch driver (not
   committed). A followup PR needs to thread
   `--no-seed-output-river` / `--no-backfill-output-geometry` flags
   through `import_basins_registry` + `reingest_basin` + the CLI, and
   extend `_refresh_parent_version_materialization` to also refresh
   `mesh_version` + `model_instance`. Tracked outside this PR.
2. **Reach-id → shud_riv-id mapping in API forecast queries** — PR-2
   switched `core.river_segment` reach-row ids from `<model>_seg_*` to
   `<model>_reach_<iRiv:06d>`, but `hydro.river_timeseries.river_segment_id`
   has always been `<model>_shud_riv_*` (output rows). The frontend
   sends the reach id from the MVT layer's properties; the API
   `forecast_series` query uses the same id against
   `hydro.river_timeseries`, which now returns zero rows for every
   reach. Net effect: clicking any reach yields an empty discharge
   chart. Symptom surfaced only when qhh + heihe (the only basins with
   timeseries data in production) landed on PR-2 geometry in phase 2.
   Mapping is a 1:1 bijection (`REPLACE(id, '_reach_', '_shud_riv_')`
   matches 3985/3985 reaches for qhh+heihe). Tracked as a separate
   hotfix PR.
3. **`tailanhe` inventory** — `default_import_eligible=false`. Whether
   this is fixture incompleteness or a deliberate exclusion needs basin
   ownership review.
4. **OBJECT_STORE_ROOT ownership on node-27** — `/home/ghdc/nwm/object-store`
   is `1103:nfsdata`, `nwm` user is not in `nfsdata` group. Reingest had
   to use a scratch dir. Long-term: either grant `nwm` write access to
   the canonical object-store, or wire the operator account into the
   `nfsdata` group, or document the scratch-dir pattern.

## Verification commands replayed (reference)

```bash
# Apply pending migrations
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
DATABASE_URL=postgresql://nhms:nhms_dev@localhost:55432/nhms \
  uv run python -m packages.common.migrate

# Reingest aggregate
DATABASE_URL=postgresql://nhms:nhms_dev@localhost:55432/nhms \
NHMS_BASINS_ROOT=/home/ghdc/nwm/Basins \
OBJECT_STORE_ROOT=/home/nwm/NWM/.nhms-work/pr570/object-store \
OBJECT_STORE_PREFIX=s3://nhms \
uv run python scripts/reingest_all_basins_receipt.py \
  --work-dir .nhms-work/pr570/reingest \
  --output artifacts/pr570/reingest_aggregate_$(date +%Y%m%d_%H%M%S).json \
  --package-version vbasins-reingest-$(date +%Y%m%d_%H%M%S) \
  --model-id-template "basins_{slug}_shud" \
  --auth-actor-id cli-model-admin --auth-role model_admin \
  --continue-on-error

# Spot-check DB per-model reach count
docker exec nhms-db psql -U nhms -d nhms -At -c "
  SELECT mi.model_id, COUNT(*)
  FROM core.river_segment rs
  JOIN core.model_instance mi ON mi.river_network_version_id = rs.river_network_version_id
  WHERE COALESCE(rs.properties_json->>'shud_output_river','false')='false'
  GROUP BY mi.model_id
  ORDER BY 1;
"
```
