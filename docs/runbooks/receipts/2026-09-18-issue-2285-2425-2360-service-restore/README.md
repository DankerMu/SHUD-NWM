# 2026-09-18 node-27 maintenance service restore (#2285 / #2425 / #2360)

OpenSpec change: `openspec/changes/archive/2026-09-19-restore-node27-maintenance-services/`.
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

## Stage B — after merge, 2026-09-19

PR #2499 merged as `8942722d`. Evidence under [`stage-b/`](stage-b/); CST
timestamps in captured files are UTC+8.

### `nwm` side — [`stage-b.sh`](stage-b/stage-b.sh), 00:57:22Z → 00:57:23Z

- `/home/nwm/NWM` fast-forwarded `258b06ec` → `8942722d`
  ([`before/head`](stage-b/before/head), [`stage-b.log`](stage-b/stage-b.log)),
  worktree clean.
- Compression rebind (D7): the repo env is a `0600` copy of the fence env with
  only `REPO_ROOT` rewritten;
  [`rebind-asserts`](stage-b/after/rebind-asserts) — `REPO_ROOT=/home/nwm/NWM`,
  `PER_TICK_BOUND=2`, receipt and lock paths each count 1; `repo-env-mode`
  `600 nwm`; unit budget preflight `--check` rc 0 (empty
  [`preflight.out`](stage-b/after/preflight.out)). Fence env and tree kept
  as rollback.
- `nwm` raw-retention env: `NODE27_RAW_RETENTION_LANES=raw,precip-cache`
  appended (`lanes-count` 1, `raw-env-mode` `600 nwm`).
- Manual `nwm` raw-retention tick 00:57:32Z: `Result=success`, `ExecMainStatus=0`
  ([`raw-run-state.txt`](stage-b/after/raw-run-state.txt));
  [`raw-retention-20260919T005732Z.json`](stage-b/after/raw-retention-20260919T005732Z.json):
  `production_execute`, `lanes=["precip-cache","raw"]`, deleted 4
  (`raw/{gfs,IFS}/2026090300`, `…12`), `failed=0`, lock failures all 0,
  `{"key":"canonical","reason":"lane_not_selected"}`, `cutoff=2026-09-04T00:00:00Z`.
- `OnFailure=nhms-node27-unit-failure-alert@nhms-node27-raw-retention.service.service`,
  `DropInPaths=` empty ([`raw-show.txt`](stage-b/after/raw-show.txt)).

### Canonical lane — operator `sudo`, 01:24:02Z

[`installer.out`](stage-b/after/installer.out) from
`sudo scripts/node27_canonical_retention_install.sh`: env written `600
frd_muziyao` (values not shown), timer enabled, synchronous start rc 0,
`Result=success`, `User=frd_muziyao`, lock still `-rw------- frd_muziyao`.
[`raw-retention-20260919T012402Z.json`](stage-b/after/raw-retention-20260919T012402Z.json):
`lanes=["canonical"]`, deleted 14 (`canonical/{gfs,IFS}/2026083112` …
`2026090312`), `failed=0`, lock failures all 0, `lane_not_selected` for `raw`
and `precip-cache`. Both summaries: `retention_days=14`,
`sources=["gfs","ifs"]`, same `cutoff`.
[`canonical-show.txt`](stage-b/after/canonical-show.txt): `OnFailure=` the
system alert template, `DropInPaths=` empty; timer `active`, next elapse
03:35Z ([`timers-system.txt`](stage-b/after/timers-system.txt)).

System alert template started by hand by the operator at 01:26:10Z:
[`system-alert-journal.txt`](stage-b/after/system-alert-journal.txt) holds
`SMTP-ACCEPTED code=250` and `SENT unit=nhms-node27-canonical-retention.service`
(recipient redacted). The operator confirmed the mail body quotes the unit's
real system-journal lines (`Starting …`, `Deactivated successfully`,
`Finished …` at 09:24:02 CST), not the placeholder or a permission error.

### Installed units equal the repo (#2285)

Every `diff-sys-*.txt` / `diff-user-*.txt` is empty: the three system units
and the seven `nwm` user units (raw-retention, timeseries-retention and
compression service+timer, `unit-failure-alert@`). `DropInPaths` empty for
raw-retention, the retention timer and compression; compression
`WorkingDirectory=/home/nwm/NWM`
([`compression-show.txt`](stage-b/after/compression-show.txt)). After the
compression run below, `systemctl --user --failed` lists none of the three
units.

### Compression on repo code

**Attempt 1 — 00:57:32Z → 02:02:32Z, rc 124 (wall).**
[`journal.txt`](stage-b/after/compression-attempt1/journal.txt). No receipt
(the wrapper was killed before writing one). Selection was the newest-first
pair `_hyper_9_130` (09-15) then `_hyper_9_141` (09-14). `130` committed
(parent 24 kB afterwards); `141` rolled back when its backend saw EOF at
02:03:57Z ([`catalog-after.txt`](stage-b/after/compression-attempt1/catalog-after.txt));
no orphan compressed relation (6 compressed chunks, 6 `compress_hyper_10_*`
relations). `130` alone took 47 min for a 23 GB chunk (≈55 s/GB predicts
~21 min). [`timeline.txt`](stage-b/after/compression-attempt1/timeline.txt):
the #1988 I9 session `i9-predeploy-readonly` connected 1 s after the
compression session and held its connection until 01:44:19Z; `130`
committed 15 s later, at 01:44:34Z. Ten other `i9-*` read-only sessions ran
00:57–01:47Z, several scanning `river_timeseries_legacy` until statement
timeout, alongside an autopipe ingest
([`app-counts.txt`](stage-b/after/compression-attempt1/app-counts.txt)).
`log_lock_waits` is `off`, so a lock wait behind that open read-only
transaction is **not established**; the correlation is recorded, the cause
is not.

**Attempt 2 — 02:10:00Z → 02:33:35Z, `Result=success`.** Started from a clean
`/home/nwm/NWM` at `8942722d`
([`head.txt`](stage-b/after/compression-attempt2/head.txt)), with a
per-minute `pg_stat_activity` poll ([`poller.sh`](stage-b/poller.sh),
[`poll.sql`](stage-b/poll.sql)):
[`poll.txt`](stage-b/after/compression-attempt2/poll.txt) shows only `IO`
waits or none and `pg_blocking_pids` always `{}`.
[`compression-receipt.json`](stage-b/after/compression-attempt2/compression-receipt.json):
`mode=enforce`, `outcome=clean`, `per_tick_bound=2`,
`now_utc=2026-09-18T00:00:00Z`, `head_sha=8942722d…` (equals
`git -C /home/nwm/NWM rev-parse HEAD`); selected `_hyper_9_141` (09-14,
23.04 → 6.14 GB) and `_hyper_9_143` (09-13, 23.04 → 6.14 GB), both
`committed`; deferred descends 09-12 → 09-02. `143` ran 02:14:27Z → ~02:33:35Z,
about 50 s/GB. `141` took ~4.4 min, because attempt 1 had already read it
into cache.

Bound 2 is kept. Stage A, the 2026-09-17 tick and attempt 2 all measured
≈50–55 s/GB. Attempt 1 ran with identified concurrent operator sessions.
Conditional: if a timer tick with no concurrent operator DB session hits
rc 124, the D2 derivation is wrong and the bound drops to 1.

The legacy `_hyper_3_110` (558 GB) risk in the tier runbook stands: on
2026-09-19 `river_timeseries_legacy` still has 4 chunks, and #1988's
production DROP had not run yet. The maintenance runners discover hypertables
at runtime and include `*_legacy` only while it exists
(`packages/common/node27_timeseries_discovery.py`), so the DROP needs no
unit or env change.

### Housekeeping (operator-requested, outside the change)

At the operator's request, 16 leftover transient `nwm` user units were
unloaded at 01:28Z: 12 `failed` one-offs (`reset-failed`) and 4
`active (exited)` one-offs with no `ExecStop` and no process (`stop`), plus
one orphan runtime drop-in. These were one-offs from issues #1987, #2349,
#2370, #2373, #2374 and #2382, plus reslice and pgdata-prepare runs, all ended
2026-09-10 → 09-15. The `systemctl cat`/`show` snapshots stay on node-27
under `/home/nwm/tmp/svc-restore/unit-cleanup/`; they are not copied here
because `show` output carries unit environments. `nhms-node27-resource-governance.service`
was left `failed`: its 2026-09-18 tick reported
`RESOURCE_GOVERNANCE_CRITICAL:AUTOVACUUM_OUTPUT_STALLED`, which #1770 tracks.

### Still open

- E6 (#2425 AC 1/3): the next two daily compression and retention ticks,
  posted to #2425.
- E7 (#2425 AC 4): D11 live measure over a compressed `river_timeseries`
  chunk, posted to #2425 before it closes.
