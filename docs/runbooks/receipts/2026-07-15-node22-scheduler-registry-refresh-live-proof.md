# Node-22 scheduler registry refresh live proof — 2026-07-15

This receipt closes the live evidence floor for issue #1076. It binds the
production recovery to implementation commit
`96b7511e3bada1a5ce707aada45de4a4726f8cff`. No product-archive retention,
compression, salvage, drill, or #856 cascade command was executed.

## Provider refresh

The controlled node-22 window began with both scheduler and provider-refresh
timers stopped, both services inactive, an empty Slurm queue, a clean
`feat/issue-1076-scheduler-registry-refresh` checkout, and no refresh process.
The production Basins truth source contained 19 registered models after the
operator removed the duplicate `HHe-MAIN-02`; the registry contains only
`basins_hhe_shud` for HHe.

The final full refresh ran naturally from
`2026-07-15T04:12:40.641956Z` through
`2026-07-15T04:35:32.627814Z`:

- run: `refresh_20260715T041240Z_50ef83266650`
- receipt: `nhms.scheduler.file_provider_refresh_receipt.v1`
- outcome/phase/reason: `published` / `complete` / `success`
- `database_free=true`; stderr was empty; orphan total 0; residues empty
- registry: 19 entries; shared and private worker mirror are byte-identical;
  SHA-256 `012d6e71661ec6b5c67e24db0fcf9bde00b434e5c4be8b918afe7510ee6521b7`
- readiness: 38 entries (19 GFS + 19 IFS); SHA-256
  `ffcebd39c854aa49a5810b5f26459eaf91a981f46d7f15ddfc4f0dab2afac486`
- state: 529 entries; SHA-256
  `c586bd877fd97399f8538230a2d5751e07a05f7a3b39ac0ce4078ae9f38b35ed`
- receipt/latest SHA-256:
  `bc08df97911d21e394c5e693b74773f156ea4c3f63bebcf3c3ebf130ba48cd9c`

This was a full validator/publisher path, not a timestamp edit: the registry
publisher rediscovered all 19 models and attempted all 19 immutable packages,
the readiness publisher rebound 38 catalog entries, and the state repository
revalidated all 529 referenced objects. The wrapper removed all libpq selectors
before starting the Python process; the accepted receipt independently records
the DB-free boundary.

Raw operator evidence is retained outside Git at:

`/users/frd_muziyao/issue-1076-live/20260714T124318Z-c468cc58dde3/attempt6-final-96b7511e-19models`

## Authentic scheduler and Slurm chain

The scheduler planned the selected identity without
`db_free_registry_blocked`:

- source/cycle/model: `gfs` / `2026-07-06T12:00:00Z` /
  `basins_dth_ls_shud`
- pipeline run: `cycle_gfs_2026070612_convert_basins_dth_ls_shud`
- hydro run: `fcst_gfs_2026070612_basins_dth_ls_shud`
- authorized manual repair attempt: 4, recorded through the canonical
  `pipeline.retry_run` policy path after the earlier permanent `NODE_FAILURE`
- pre-submit pass: `scheduler_2026071503_4751c02bdd6b`, one candidate, zero
  blockers, restart stage `forcing`
- submitting pass: `scheduler_2026071503_492c29346bfc`

All four production stages terminated naturally:

| Slurm job | Stage | State | Exit | Elapsed |
| --- | --- | --- | --- | --- |
| 14583 | convert | `COMPLETED` | `0:0` | 00:00:34 |
| 14584 | forcing | `COMPLETED` | `0:0` | 00:04:10 |
| 14585 | forecast | `COMPLETED` | `0:0` | 00:00:34 |
| 14586 | state_save_qc | `COMPLETED` | `0:0` | 00:00:06 |

The stages created a new forcing tree, run tree, and state checkpoint. The
first copyback correctly failed closed because the shared state index referenced
private-root objects. Two production findings were fixed without rerunning SHUD:
split-root reference validation now uses the private authority, and copyback
conflict comparison excludes only validator-derived evidence. The exact
successful run was then copied back: 290 files, 44,399,386 bytes, 529 merged
state entries, 472 checkpoints copied, and 57 reused. A subsequent read-only
plan, `scheduler_2026071504_ddaaa228e358`, returned zero candidates, zero
blockers, and `terminal_pipeline_success` for this identity.

Raw scheduler evidence is retained at:

`/users/frd_muziyao/issue-1076-live/20260714T124318Z-c468cc58dde3/scheduler-pass-attempt2-19models`

## Node-27 NFS and ACL proof

Node-27 observed byte-identical final provider hashes through
`/home/ghdc/nwm/object-store`. It read all 529 state references with zero
unreadable objects. The real writer leaves were:

| Lane | Relative path under `/home/ghdc/nwm/object-store` | Mode | SHA-256 |
| --- | --- | --- | --- |
| forcing | `forcing/gfs/2026070612/basins_dth_ls_vbasins/basins_dth_ls_shud/forcing_package.json` | 0644 | `1cb553d3caf889b6aacd0cab0e0332c31ad0da340f11bdcfc02104b908f7cf5e` |
| run input | `runs/fcst_gfs_2026070612_basins_dth_ls_shud/input/manifest.json` | 0644 | `0c9984b7dd223e702d6b4e1d6a1a783052b5ccac46f2eae4f290aeb6974087bb` |
| run output | `runs/fcst_gfs_2026070612_basins_dth_ls_shud/output/CJ-DTH-LS.rivqdown.csv` | 0644 | `9bbe811f717ab8c7988af108c23484dc641dd05fa5fa4940d5f9587dd5f51545` |
| state | `states/gfs/basins_dth_ls_shud/2026070700/gfs_2026070612/f012/state.cfg.ic` | 0664 | `b494383d711237b0048d3209bb1f151c0922dbdb2d524f5041dabfe794caa83e` |

Node-22 reports numeric ownership `1103:1078` (`frd_muziyao:huser`); node-27
maps the same NFS group to `nfsdata` and does not resolve UID 1103 by name.
The forcing, run, and state lane directories each expose the same inherited ACL:
owner `rwx`, named user `nwm:rwx`, group `r-x`, mask `rwx`, other `r-x`, plus
default owner `rwx`, default `nwm:rwx`, default group `r-x`, default mask `rwx`,
and default other `r-x`. Direct reads as `nwm` succeeded for every leaf above.

## Restored steady state

The final receipt was validated against the current three provider bytes before
timer mutation. The issue-owned controlled freeze was then restored to the
installation baseline:

- `nhms-compute-scheduler.timer`: enabled / inactive
- `nhms-compute-scheduler.service`: static / inactive
- `nhms-scheduler-file-provider-refresh.timer`: enabled / active; next tick
  `2026-07-16 10:22:06 CST`
- `nhms-scheduler-file-provider-refresh.service`: static / inactive
- refresh processes: 0; issue-owned Slurm jobs remaining: 0

The installer returned `enabled_active` with `scheduler_unchanged=true`. This
leaves the daily DB-free refresh lifecycle enabled while preserving the
scheduler timer's pre-window state.
