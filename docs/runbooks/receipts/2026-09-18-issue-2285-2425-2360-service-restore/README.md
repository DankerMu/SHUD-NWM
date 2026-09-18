# 2026-09-18 node-27 maintenance service restore (#2285 / #2425 / #2360)

OpenSpec change: `openspec/changes/restore-node27-maintenance-services/`.
Host: node-27 (`nwm`, user manager). DB container `nhms-db`. No env value is
reproduced here; env files were edited by key only.

## Failure census (2026-09-18 ~13:50Z, read-only)

| unit | exit | cause |
|---|---|---|
| `nhms-node27-raw-retention.service` | 1 | summary `raw-retention-20260918T033532Z.json`: `copyback_lock_failures.lock_unsafe=10`, canonical cycles `CopybackLockError` EACCES on `.nhms-copyback-batch.lock` (`-rw------- frd_muziyao`); raw lane deleted 4 cycles |
| `nhms-node27-timeseries-compression.service` | 124 | wrapper wall 3900 s: 04:25:32Z start → 05:30:32Z exit; `_hyper_9_150/151/152` compressed, `_hyper_9_153` rolled back; no receipt written that tick |
| `nhms-node27-timeseries-retention.service` | 1 | `RETENTION_CONCURRENT_INVOCATION` at 05:15:00Z (compression still running); installed timer 05:15Z vs repo 06:36Z |

`stage-a/before/state.txt` is the `systemctl --user show` capture of these three
units; `stage-a/before/*.service|*.timer` are the installed unit files.

## Stage A — stop-gap, 2026-09-18T13:57:23Z → 13:57:24Z

Script: [`stage-a/stage-a.sh`](stage-a/stage-a.sh) (operator-approved: install
repo units, immediate retention catch-up, bound 2).

- Fence env (`~/.local/state/issue1895-maintenance-retirement-95481481/config/node27-timeseries-compression.env`):
  backup `…env.bak-bound4-20260918` (same 0700 dir), only the bound line
  changed — `bound2-count`=1, `env-changed-lines`=2 (one `<`, one `>`),
  `env-mode`=`600 nwm`; the unit's own budget preflight `--check` →
  `preflight.rc`=0.
- Installed the repo `nhms-node27-raw-retention.service` and
  `nhms-node27-timeseries-retention.timer`; `diff` installed vs repo → both
  `diff-*.rc`=0, empty `diff-*.txt`. `daemon-reload`; `reset-failed` on
  compression + retention.
- Retention timer restart → `Persistent=true` catch-up of the refused tick:
  `Result=success`, `ExecMainStatus=0`;
  [`retention-20260918T135724Z.json`](stage-a/after/retention-20260918T135724Z.json):
  `reference_time=2026-09-17T12:00:00Z`, `cutoff=2026-08-27T12:00:00Z`,
  dropped `_hyper_9_126`, `_hyper_3_62`, `_hyper_1_61`. Next elapse
  2026-09-19 06:36Z.
- Manual compression start (`systemctl --user start --no-block`, fence code,
  bound 2) 13:58:50Z → 14:36:11Z, `Result=success`, `ExecMainStatus=0`;
  [`compression-receipt-20260918T143611Z.json`](stage-a/after/compression-receipt-20260918T143611Z.json):
  `outcome=clean`, `per_tick_bound=2`, `now_utc=2026-09-17T12:00:00Z`,
  `head_sha=95481481…` (fence), selected `_hyper_9_153` (20.16 → 5.41 GB) and
  `_hyper_9_154` (20.97 → 5.60 GB), both `committed`: 41.13 GB in 2241 s =
  54.5 s/GB.

State capture 2026-09-18T15:20:04Z (`systemctl --user show` of the three units
and journal 21:50–22:40 CST):
[`stage-a/after/state-after-stage-a.txt`](stage-a/after/state-after-stage-a.txt) —
retention and compression `Result=success` / `ExecMainStatus=0` with the start
and exit timestamps above; raw-retention still `exit-code` / 1 from 03:35:32Z.

Catalog after Stage A (read-only transaction): `river_timeseries` 5/29
compressed, 14 eligible uncompressed at W−2 d; `river_timeseries_legacy` 2/4
(uncompressed `_hyper_3_110` 558 GB range_end 09-17, `_hyper_3_113` 108 GB);
`forcing_station_timeseries` 2/5.

Still failing after Stage A: `nhms-node27-raw-retention.service` (canonical
lane identity — needs the merged code + the operator's sudo step, Stage B).

## Pre-merge live plan-only check — 2026-09-18T15:02Z

Scratch worktree `/home/nwm/tmp/wt-2425` at `afa641ce` (the PR code), live
`nwm` env file, `NODE27_RAW_RETENTION_PLAN_ONLY=true`, summaries redirected to
`/home/nwm/tmp/svc-restore/planonly/` (production log dir untouched). Script:
[`pre-merge-plan-only/planonly.sh`](pre-merge-plan-only/planonly.sh).

| `LANES` | rc | summary |
|---|---|---|
| `raw,precip-cache` | 0 | `plan_only`, `lanes=["precip-cache","raw"]`, 2 raw planned, `{"key":"canonical","reason":"lane_not_selected"}`, lock failures 0 |
| `canonical` | 0 | `plan_only`, `lanes=["canonical"]`, 12 canonical planned, `lane_not_selected` for `raw` and `precip-cache`, lock failures 0 |
| `raw,canon` | 2 | `preflight_blocked`, blocker `{"field":"lanes","reason":"unknown_lane","value":["canon"]}`, nothing planned |

Both completed summaries carry `cutoff=2026-09-03T12:00:00Z`. As `nwm` the
canonical plan succeeds because plan-only takes no lock; the deleting tick
needs uid 1103 (Stage B).

## Pre-merge compression dry run — 2026-09-18

Same worktree; a temporary 0600 copy of the fence env with only `REPO_ROOT`
rewritten to the worktree (deleted after the run), receipt/lock redirected.
Script: [`pre-merge-dry-run-compression/drycomp.sh`](pre-merge-dry-run-compression/drycomp.sh).
rc 0; [`dry-receipt.json`](pre-merge-dry-run-compression/dry-receipt.json):
`mode=dry-run`, `outcome=clean`, `per_tick_bound=2`,
`now_utc=2026-09-17T12:00:00Z`, `head_sha=afa641ce…` (the worktree — the
`head_sha` proof that D7 relies on works). Selected newest-first:
`_hyper_9_130` (range_end 09-15), `_hyper_9_141` (09-14); deferred continues
09-13 → 09-02 in descending order. Under the old order the same catalog would
select `_hyper_9_155`/`_hyper_9_156` (09-02/09-03), the next retention targets.

## Stage B — after merge

Planned per design D8; receipt appended here after execution.
