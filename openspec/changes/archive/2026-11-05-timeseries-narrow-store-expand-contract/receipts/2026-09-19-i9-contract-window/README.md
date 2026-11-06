# I9 / task 6.2 contract window (production, node-27, 2026-09-19)

Authorizer Danker ("好的，授权生产go"; merges pre-authorized). Executor Main.
Migration `db/migrations/000060_river_timeseries_contract.sql`, sha256
`b885aac784e70d8cf3a26f57e70e15a5cb8d1e3654116afbac07cc20be0ce8c6` — verified on
node-27 after the pull to be byte-identical to the artifact CI and the local
contract tests exercised; the window aborts on mismatch.

Window script `window.sh` (sha256 `bbdd6809c0ffca1c…`), launched detached with
`setsid nohup`, output to `/home/nwm/tmp/i9-window.log`.

## 1. Timeline (CST)

| | |
|---|---|
| window open | 16:06:32 |
| quiet window begins (API down, `root=000`) | 16:06:36 |
| migrate starts | 16:06:50 |
| migrate ends, rc=0 | 16:07:35 |
| API active again | 16:07:36 |
| window closed | 16:07:43 |

**Quiet window 67 s; the migration itself 45 s.** `000060` is one top-level `DO`
statement, so 45 s is its single-statement wall time, not a sum of internal DDL.

Tree `8942722d` → `40593a99` by `git pull --ff-only` on a clean working tree.

## 2. Gates, all measured inside the window

- **user-systemd reachable from the detached context** — asserted before
  anything was stopped. `XDG_RUNTIME_DIR=/run/user/1005`. Without this, every
  `systemctl --user` would fail quietly, the API would still hold
  `AccessShareLock` on `core.river_segment` during the drop, and the restore
  trap would fail too. Abort path removes the trap so a refusal restores nothing.
- **API actually stopped** — `is-active` = `inactive` asserted before the pull;
  `curl` to `:8080/` returned `000`.
- **Retention gate re-measured at execution time** (it moves daily; the morning's
  reading is not evidence for 16:06): **in-window legacy-routed runs = 0**,
  total legacy-routed = 2919. Deployed
  `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=21`, equal to the migration's
  hardcoded floor, so no GUC override was needed.
- **Pending set enumerated**, per `docs/runbooks/tier-node27-timeseries-storage.md:6207`
  ("do not run accumulated master migrations blindly"):
  `PENDING = ['000060_river_timeseries_contract.sql']` — exactly one.
  Ledger 60 rows / 54 files on disk; the surplus is the #2048 retroactive-deletion
  drift, already recorded, and `migrate.py` only walks files on disk.

## 3. The migrate lane is the superuser lane — measured, not assumed

Running the migration under `node27-ingest.env` **would have failed**, and this
was measured read-only before the window rather than discovered during it:

| object 000060 drops | owner |
|---|---|
| `hydro.river_timeseries_legacy` | `nhms_ingest_rw` |
| `hydro.hydro_run` (column) | `nhms_ingest_rw` |
| `hydro.cutover_river_identity_normalization()` | **`nhms`** |
| `hydro.verify_river_identity_normalization()` | **`nhms`** |

`pg_has_role(nhms_ingest_rw, nhms, 'USAGE') = false`, and `DROP FUNCTION IF
EXISTS` skips only a *missing* function, not one owned by someone else. The DO
block is atomic, so the catalog would have been unchanged — but the outage would
have been spent for nothing and the run would have landed in the
"failed migration, do not rerun blindly" corner.

`nhms` is `rolsuper = true`; `scripts/node27_provision_write_roles.sh:30` and
`docs/runbooks/current-production-ops.md:2427` already designate it as the
migration lane. The window therefore uses two lanes: read-only gates on
`nhms_ingest_rw` (least privilege), migrate and post-state verification on
`nhms`. Verification is on the superuser lane because `nhms_ingest_rw` gets
`permission denied for table schema_migrations`, which would have aborted the
verify block on its first statement and left the window with no post-state
evidence.

A preflight inside the window asserts `superuser`, `current_database() = nhms`,
the legacy table's presence and the pending set, and refuses before anything
irreversible if any of them is wrong.

## 4. Post-state

```
legacy_table_gone=True  routing_column_gone=True  functions_left=0  narrow_present=True
ledger_rows=61   tail: ['000060_river_timeseries_contract.sql',
                        '000059_river_timeseries_narrow_expand.sql',
                        '000058_hot_timeseries_chunk_interval_3d.sql']
narrow indexes: ['river_timeseries_narrow_pkey',
                 'river_ts_run_discovery_key_idx',
                 'river_ts_segment_time_key_idx']
run_display_coverage (rows, populated): (7935, 7025)
database size: 480 GB     hydro.river_timeseries: 400 GB
to_regclass('hydro.river_timeseries_legacy') -> None
```

`scripts/node27_provision_write_roles.sh` (runbook §9.6 / #1774, mandatory after
any migrate) exited **0**: `CREATE on any schema for the write roles: (none)`
for both write roles, `audit: OK -- no owner drift`, `full provision complete;
audit clean`.

No `df` baseline for `/data/GHDC` was captured before the window, so **no disk
delta is claimed here**; the database-level facts above are the evidence.

## 5. The narrow read path is byte-identical across the window

A real `forecast-series` request replayed from `/tmp/display-api.log` — the
narrow read path, i.e. the thing this migration could have broken — not `/` or
`/ops`, which are HTML shells and prove nothing about the store:

| | before (16:0x) | after (16:07:43) |
|---|---|---|
| status | 200 | 200 |
| bytes | 6075 | 6075 |
| sha256 (first 16) | `a48605688b2e09c4` | `a48605688b2e09c4` |

Also after: `root=200`, `ops=200`, 9 timers restored.

## 6. The `timescaledb_210` case finally ran on its oracle

`tests/test_river_timeseries_contract_integration.py` carries one case CI
deselects by design — `test_contract_drops_a_legacy_hypertable_carrying_compressed_chunks`,
the only one that reaches into `_timescaledb_catalog.chunk.compressed_chunk_id`
and is therefore TimescaleDB-version-coupled. Until now it had never run
anywhere; "8 tests" in the 6.2 PR meant 7 executed and 1 routed.

Run after the window on node-27 (PostgreSQL 15.2, TimescaleDB **2.10.2**), each
case building and dropping its own `nhms_it_<uuid>` database, production `nhms`
objects untouched:

```
collected 8 items
tests/test_river_timeseries_contract_integration.py ........             [100%]
======================== 8 passed in 154.24s (0:02:34) =========================
```

Eight dots, no `s`: this is not the silent-skip trap (`tests/conftest.py:164-197`)
and not a `--collect-only` degradation. Afterwards: no leftover `nhms_it_*`
databases, no leftover sessions of mine, display API and all 9 timers active.

## 7. `yd-NWM` was never touched

`yd-display-api.service` `MainPID = 3163766` before the window and `3163766`
after, `SubState=running` throughout. The script's unit list contains only
`nhms-node27-*` and `nhms-display-api`; no `yd-*` unit is named anywhere in it.

## 8. Exposure accepted, and one bound that is stated rather than measured

2919 legacy-routed runs lost their last physical copy. All were out of the
21-day retention window and already dark to every read path since 6.3 went
process-live at 09-19 09:44:48 — see
`receipts/2026-09-19-i9-deploy-state-and-exposure/`.

Beyond those, a pre-window probe found **48 published, `narrow`-routed, in-scope
runs whose `run_display_coverage` row is populated but which have no facts in
the narrow store**. They are real and they are a defect, but not one this
migration created:

- All 48 are `fcst_*_2026082000`, 23 days old, i.e. **outside the 21-day window**
  — the spec's own expendability criterion.
- All 48 have `end_time = 2026-08-27T00:00:00Z` exactly, and
  `legacy WHERE valid_time < 2026-08-27` measured **0**. So the only legacy rows
  they could ever have had are at that single instant: **at most one timestep per
  run, ≤1/168 of it.** Narrow had already aged them out: its oldest surviving
  chunk starts at exactly `2026-08-27T00:00Z` (30 chunks, **1-day** interval,
  10 compressed — measured after the window), and the probe found nothing for
  these 48 even at that boundary. Legacy could retain at most that one instant
  because its chunks are wider and the `[08-27, …)` one straddled the cutoff.

  *Corrected in place:* an earlier draft of this paragraph said "3-day chunks"
  for narrow and "7-day" for legacy. The live narrow table is **1-day**
  (`000059` creates it with `chunk_time_interval => interval '1 day'`);
  `000058`'s 3-day interval applied to the pre-rename table — the very one
  000060 dropped. Measured on the live hypertable, not inferred from a
  migration's name.
- The **exact** count was not measured. Four attempts to probe it all hit
  `statement_timeout` against the two compressed legacy chunks (the
  decompression path is not interruptible often enough), and the cost of an
  answer on the production primary was not worth a bound already known to be
  ≤ one timestep of out-of-window data. Stated as a bound, not as zero.

Their populated-but-empty coverage rows are the #1446 overwrite guard
(`packages/common/display_coverage.py:687-689`) freezing a `segment_count > 0`
computed before the 2026-09-15 re-forward re-decided routes. That is a
**pre-existing** defect, live in production today, unchanged by this migration —
filed separately, see §10.

## 9. Errors made and corrected during this window's preparation

- Planned to run `migrate.py` under the ingest DSN. Wrong: see §3. Caught by a
  read-only ownership query before the window, not during it.
- Put the pending-set gate on the ingest lane, where
  `public.schema_migrations` is `permission denied`. Moved to the superuser
  preflight.
- Left the verify block on the ingest lane for the same reason. Moved.
- `cmd | tail` followed by `$?` yields `tail`'s status, which would have reported
  a failed migration as `exit: 0`. Fixed with `set -o pipefail` and explicit
  `mig_rc` / `roles_rc` capture.
- Two probe backends were left running against `hydro.hydro_run`; they hold
  `AccessShareLock`, which `ALTER TABLE … DROP COLUMN` would have queued behind
  and `lock_timeout=5s` would have failed. Terminated and confirmed
  (`locks on hydro.hydro_run: (none)`) before the window opened.
- An earlier unbounded legacy probe (no `valid_time` bound) destroyed chunk
  exclusion and timed out 48/48; the bounded form is the only one that plans.
- **My read-only probing broke a production retention run.** At 14:36:08 — two
  hours before the window, while orphaned `i9-containment` backends of mine were
  running 1400–1600 s against the narrow hypertable's chunks —
  `nhms-node27-timeseries-retention.service` tried to drop
  `_timescaledb_internal._hyper_9_150_chunk`, waited **334 925 ms**, and exited
  1 on `RETENTION_DROP_FAILED:hydro._hyper_9_150_chunk: lock-contention(55P03):
  canceling statement due to lock timeout`.

  That chunk is `hydro.river_timeseries` `[2026-08-27, 2026-08-28)` — the oldest
  narrow chunk, 23 days old and legitimately outside the 21-day window. It is
  still present, so retention is one chunk behind. A read-only query holds
  `AccessShareLock` on the chunks it scans; `drop_chunks` needs
  `AccessExclusiveLock`; the two do not coexist.

  Attribution is strong but circumstantial, and is stated as such: the failure
  reason is unique to 2026-09-19 (the unit's only other recent failures,
  09-14 and 09-18, are `RETENTION_CONCURRENT_INVOCATION`, an unrelated
  pre-existing race), the wait lands inside my probing window, and the chunk is
  one my probes scanned — but no lock-graph snapshot was captured at 14:41, so
  this is not a recorded lock holder.

  **Amended 2026-09-19 ~16:52 CST — remediated at the authorizer's direction.**
  The paragraph originally standing here said the unit was left `failed` and
  that retention would not be triggered by hand. Danker then directed both the
  drop and the unit's restoration, so that is no longer what happened and the
  original wording is superseded rather than left to read as fact:

  - `_hyper_9_150_chunk` was dropped through the gated
    `scripts/node27_timeseries_retention.py`, not a hand-written `drop_chunks`:
    680 ms, `freed_bytes` 4 291 829 760. Narrow went 30 → 29 chunks (oldest now
    `2026-08-28`, compressed 10 → 9), 400 GB → 396 GB, database 480 GB → 476 GB.
    `_hyper_9_151` `[08-28, 08-29)` was **not** eligible: the script's own cutoff
    is `2026-08-28T12:00:00Z` (`reference_time` `2026-09-18T12:00:00Z` minus 21
    days), stricter than a naive `now() - interval '21 days'`, which had
    suggested two chunks.
  - The replayed `forecast-series` probe after the drop is still **byte-identical**
    to the pre-window baseline: 200 / 6075 B / sha `a48605688b2e09c4`.
  - The unit was restored by **running it once to a real success**
    (`exit-code failed` → `success inactive`, `{"mode": "enforce", "outcome":
    "enforced"}`, nothing eligible so nothing dropped), not by `reset-failed`.
    Clearing the flag would have proved nothing; a clean run proves the unit
    works. Timer active, next elapse 2026-09-20 14:36 CST.

  **A defect surfaced doing this, and it is not cosmetic.** The drop was issued
  as `--dry-run`. It enforced anyway: `scripts/node27_timeseries_retention.py:462-468`
  never reads `args.dry_run` — `--enforce` wins, otherwise
  `NODE27_TIMESERIES_RETENTION_ENFORCE` decides, and the deployed env file sets
  it. The mutually-exclusive group makes `--dry-run` *look* like an opt-out when
  it only blocks `--enforce`, and its help text still reads "dry-run (default)".
  This is already filed as **#2355**; today's occurrence is recorded there as the
  first real hit. No loss resulted only because the chunk was one the authorizer
  had just asked to delete and retention was owed anyway — intent and outcome
  coincided; the mechanism did not protect anything.

## 10. Out of scope, carried

- **Stale populated coverage for the 48 runs above** — the #1446 guard keeps a
  `segment_count > 0` that no longer describes any stored fact, so the UI offers
  a curve that comes back empty. Pre-existing, independent of 000060.
- **Retention has now run against the post-contract state; compression has not.**
  Retention was hand-started at the authorizer's direction after the window (see
  §9) and succeeded with the legacy hypertable absent — so its lane walk tolerates
  the drop on live production, not only in the contract tests. The **compression**
  timer still last fired at 12:25, before the window; its next tick is
  2026-09-20 12:25 and it has **not** been triggered by hand — that standing
  constraint is untouched, and the "legacy gone" compression path remains verified
  by `test_contract_drops_a_legacy_hypertable_carrying_compressed_chunks` on the
  node-27 oracle rather than by a production tick.
- `nhms-node27-timeseries-retention.service` also failed on 09-14 and 09-18 with
  `RETENTION_CONCURRENT_INVOCATION` — a pre-existing race between overlapping
  invocations, unrelated to this epic.
- `/tmp/display-api.log` is unrotated at ~96 MB on a root volume at 82%.
- ~12 `failed` one-shot units on node-27.
- `apply_migrations_from_zero`'s `name[:6] > through` string comparison breaks at
  `001000`.
- Post-contract HTTP/MVT-layer smoke coverage gap.
