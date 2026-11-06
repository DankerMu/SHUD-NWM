# I14 forcing contract window — execution receipt (fixture I14-1993-task82.md D6)

Status: **executed, post-state verified.** `000066_forcing_timeseries_contract.sql` is applied on node-27.

## Context

- node-27's 14T volume was retired; the cluster was rebuilt on 2026-11-05 by a fresh initdb on `/home`
  (`/home/nwm/nhms-primary/pgdata`, #3054) and re-ingested at cycle 2026-10-12 00Z only. So the legacy table the
  migration drops held **32 kB / 0 rows** (not the 32 GB of the 2026-10-11 measurement), and every node-27 timer
  was already inactive and disabled before the window.
- Owner approval: 2026-11-06, in the operator session, covering (a) running the §4.12.4 window now and (b) accepting
  shape-gate reader 8 (`qhh_production_bootstrap.dynamic_forcing_count`, see `../2026-10-11-i14-forcing-shape-gate/`)
  as a documented exception: its plan reads neither object `000066` drops.
- The same window synced `/home/nwm/NWM` from `b0831d6ce` to `594912c7b` (master, includes #3053 / #3055), so the
  routing-free code (gate 3) was what started afterwards. The display API was stopped from before the pull until
  after the migrate.

## Timeline (UTC, 2026-11-06)

| time | step |
|---|---|
| 00:39:16 | preconditions: clean tree; `HEAD` is an ancestor of `origin/master` (`head.txt`) |
| 00:39:21 | superuser DSN identified as `nhms@nhms` (never printed); timers recorded (`timers-before.txt`: all 5 `inactive disabled`) |
| 00:39:21 | `nhms-display-api.service` stopped |
| 00:39:22 | `git pull --ff-only` → `594912c7b`; `uv sync --locked --all-extras --dev` (sqlalchemy 2.0.49 → 2.1.1, greenlet removed) |
| 00:39:34 | gates (`gates.txt`): gate 1 = 0, gate 4 = 0, legacy 32 kB; pending set by file = exactly `000066` (`pending.txt`); drain: 0 lock holders on the three relations, 0 idle-in-transaction |
| 00:39:35 | migrate with `lock_timeout=5s`, `statement_timeout=2min` (runner guard lines in `migrate.log`): 1 applied, 59 skipped |
| 00:39:35 | post-state (`post-state.txt`): `t\|t\|t\|1` (legacy table gone, routing column gone, narrow present, ledger row); indexes `forcing_station_timeseries_narrow_pkey`, `forcing_ts_version_variable_time_key_idx` |
| 00:39:37 | `scripts/node27_provision_write_roles.sh` full mode: "full provision complete; audit clean" |
| 00:39:37–56 | frontend rebuilt in place (old `dist` backed up to `/home/nwm/fe-dist-backups/dist.bak-20261106-pre-master-sync`); bundle `index-pS9_A1i5.js` |
| 00:39:56 | display API started |

Timers restored: none (none were active before the window; `timers-before.txt`).

## Post-window checks (display API on `594912c7b`)

- `/api/v1/basins?has_display_product=true` → 200, 66 basins; public `https://test.nwm.ac.cn/` serves `index-pS9_A1i5.js`.
- `/api/v1/mvp/qhh/latest-product?source=gfs` → 200, run `fcst_gfs_2026101200_dg_0883c7e9c1006c6fd347df500315e9df`.
- `/api/v1/met/stations/{station}/series?model_id=…&source_id=gfs&cycle_time=2026-10-12T00:00:00Z` → 200, 5 series.
- `/api/v1/layers/discharge/cycles?source=gfs|ifs` → 200, unchanged shape (`cycles: []`: the only cycle is older than
  the 12-day lookback; this predates and is independent of the window).
- Autopipe (`--seed-only --direct-grid-only --only-cycle 2026101200`, routing-free code) completed rc 0: 66
  `already_seeded`, 0 failed, `core.model_instance` activation state byte-identical before/after. An ingest tick has
  not run yet: no new cycle exists until compute resumes.

## Files

`head.txt`, `timers-before.txt`, `gates.txt`, `pending.txt`, `migrate.log`, `post-state.txt` — copied verbatim from
the private receipt directory `/home/nwm/tmp/forcing-contract-20261106T003916Z/` on node-27.
