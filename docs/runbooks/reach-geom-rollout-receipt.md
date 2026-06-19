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

9 of 11 models now hold PR-2 reach-level geometry. qhh/heihe stay on
pre-PR-2 seg-derived geometry until the QHH-bootstrap-vs-generic-reingest
contract issue is resolved (see remediation above).

## Browser live verification

> Pending — Chrome extension not connected at receipt time. Plan: capture
> ≥ 3 basins (hetianhe, keliya, zhaochen/BST or qinyijiang) at the
> previously-affected zoom levels, plus segment-level hover/popup to
> confirm crosswalk wiring. qhh + heihe browser screenshots remain
> reachable as **baseline** evidence (their old seg-derived geometry is
> still on display).

Screenshots will land under `docs/runbooks/receipts/` with filenames
encoding `<basin>-<date>-z<zoom>-lng<lng>-lat<lat>.png` per PR 4 spec.

## Issues uncovered that fall outside PR 3 / PR 4 scope

1. **QHH-bootstrap vs generic reingest output-row contract** — PR #569
   acknowledged the divergent `properties_json` digest but did not
   provide a bridging purge. Recommend a follow-up issue: either teach
   `_delete_legacy_seg_rows` to also drop QHH-bootstrap-written output
   rows, or add a `nhms-model reingest-basin --force-output-rewrite`
   flag for basins whose output history differs from the generic path.
2. **`tailanhe` inventory** — `default_import_eligible=false`. Whether
   this is fixture incompleteness or a deliberate exclusion needs basin
   ownership review.
3. **OBJECT_STORE_ROOT ownership on node-27** — `/home/ghdc/nwm/object-store`
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
