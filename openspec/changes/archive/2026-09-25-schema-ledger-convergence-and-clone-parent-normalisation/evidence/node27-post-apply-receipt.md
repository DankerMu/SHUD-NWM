# node-27 production apply receipt: schema-ledger-convergence-and-clone-parent-normalisation (PR #2635)

- **Merge:** `4b7d1f43f` (PR #2635).
- **Go-ahead:** the user authorized the production window on 2026-09-25 (tasks 5.2).
- **Operator log:** `/home/nwm/tmp/m/window.log` on node-27.

## Pre-apply (tasks 5.1, 12:42:01Z, checkout `2024a5e4`, read-only)

| item | value |
|---|---|
| `flood` rows (`flood_frequency_curve` / `return_period_result` / `run_product_quality`) | 0 / 0 / 0 |
| `pg_depend` on `flood` | 3 `pg_class`, 2 `pg_default_acl` |
| TimescaleDB on `flood.return_period_result` (jobs / compression / chunks / caggs) | 0 / 0 / 0 / 0 |
| six `hydro_run` partial indexes | stale predicates (design Context), all `indisvalid` |
| `hydro.run_status` | 12 labels, `frequency_done` after `parsed` |
| `public.schema_migrations` | 62 rows |
| backup | `/home/nwm/tmp/m/flood-pre-000064.dump`, 19018 B, sha256 `c93049945a16be5e2eacc09e096effa99d620acd34023aa2dc81bbaff05416b5`; `pg_restore --list` lists 30 TABLE/INDEX/CONSTRAINT/TRIGGER entries |

## Window (tasks 5.3)

| step | time (UTC) | result |
|---|---|---|
| 1. stop `nhms-node27-{autopipe,download}.timer`, drain | 12:42:48 | both `inactive`; no service in flight |
| 2. strict audit-only, pinned `e340dbc10` roles SQL (file, non-empty check) | 12:42:56 | rc 0; `223 expression(s)/trigger(s) scanned, 21 distinct function(s) referenced, 0 untrusted` |
| 3. `git status --porcelain` empty; `git pull --ff-only` | 12:43:05 | `2024a5e4` → `4b7d1f43` |
| 4. `packages.common.migrate`, attempt 1 | 12:43:07 | stopped before connecting: `DATABASE_URL is required` (not in `/home/nwm/NWM/.env`); nothing applied |
| 4. attempt 2, `DATABASE_URL=postgresql://nhms:***@127.0.0.1:55432/nhms` (as in the #2145 receipt) | 12:43:23–12:43:24 | `lock_timeout=5s`; ledger check passed; `Applied` `000062`, `000063`, `000064`; `3 applied, 55 skipped, 58 total`; rc 0; no lock timeout |
| 5. `bash scripts/node27_provision_write_roles.sh` (full) | 12:43:32 | rc 0; `177 expression(s)/trigger(s) scanned, 20 distinct function(s) referenced, 0 untrusted`; `full provision complete; audit clean` |
| 6. restart timers | 12:43:33 | both `active` |

The `flood` drop removed 46 expressions and triggers and 1 function from the audit's scan, matching the dropped CHECKs and defaults.

## Post-apply (tasks 5.4)

**Catalog diff.** Probe `evidence/catalog_diff.py` at `4b7d1f43` (sha256 `6a6b9ca4…9777`), same command as tasks 1.1: a throwaway database built from all 58 migrations vs production.

| category | fresh | prod | ledger-only | prod-only |
|---|---|---|---|---|
| schema | 6 | 6 | 0 | 0 |
| relation | 46 | 46 | 0 | 0 |
| index | 88 | 88 | 0 | 0 |
| constraint | 89 | 89 | 0 | 0 |
| column | 385 | 385 | 0 | 0 |
| enum | 10 | 10 | 0 | 0 |
| function | 4 | 4 | 0 | 0 |
| trigger | 8 | 8 | 0 | 0 |
| hypertable | 4 | 4 | 0 | 0 |
| extension | 6 | 6 | 0 | 0 |

Before the window the same probe reported 1 / 3 / 22+6 / 26 / 59 / 1+1 / 0 / 1 / 1 / 0 differences (design Context). Production's catalog now equals the ledger's in every category.

**Other checks:**
- **Ledger check** (read-only, `nhms_display_ro`): 65 ledger rows, 58 files, `mismatches == []`, pending `[]`, 7 retired recognised.
- **The six `hydro_run` partial indexes:** all `indisvalid = t`, each with `status = ANY (ARRAY['succeeded','parsed','published'])` (plus `cycle_time IS NOT NULL` for the three candidate indexes).
- **`flood`:** `to_regnamespace('flood')` is NULL.
- **Ledger rows:**
  - `000062` at 12:43:23.35Z
  - `000063` at 12:43:24.60Z
  - `000064` at 12:43:24.71Z
- **`display_ready_run` statement, `EXPLAIN (ANALYZE, BUFFERS)`:** `Index Scan using hydro_run_latest_ready_run_idx on hydro_run h`, `shared hit=8`, 0.203 ms. Before convergence that index could not serve this predicate (#2048 evidence 1).
- **Display API:** restarted on `4b7d1f43` (`OK systemd_main_pid=1881017 workers=2`, smoke passed). `/health` 200; `/api/v1/runtime/config` 200 `display_readonly`; `/api/v1/slurm/health` 404; `/api/v1/layers` 200 with layers. No HTTP 500 or traceback in `/tmp/display-api.log` for 60 s after the restart.
- **Timers:** both `active` after the window.

## Rollback

Not needed. It would be `pg_restore` of the dump above plus `DELETE FROM public.schema_migrations WHERE version = '000064_drop_retired_flood_schema.sql'`. That restores plain tables, not the hypertable (design D3). Until `000064` has run again after such a rollback, every pre-write audit would use the pinned source.
