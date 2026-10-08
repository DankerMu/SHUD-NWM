# node-22 deployment receipt (#2749, PR #2778)

Host node-22, `/scratch/frd_muziyao/NWM`, 2026-10-08. Times are UTC unless marked CST (UTC+8). Raw files on
node-22: `/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/issue-2749-deploy-20261008T042150Z/`
(`observe.log` sha256 `f642428748d86853b3911090b7e6b89330876c3ab103469faf4324b09ebc446e`, installer stdout/stderr
per action, `start-attempt1.out`, `start.out`).

## Sequence

| time | step | result |
|---|---|---|
| 04:21 | probe `systemd-run --user --wait --pipe --collect /usr/bin/systemctl --user is-active nhms-compute-scheduler.service` | printed `activating` (exit 3): the state is readable from inside a user unit |
| 04:21 | `git status --porcelain` (only untracked `.nhms-work/`), `git pull --ff-only` | `83220d46` -> `0ae95094` |
| 04:21 | before-state | refresh timer enabled/active, refresh service static/inactive, scheduler timer enabled/active; `refresh.before` = `disabled\tinactive\nstatic\tinactive\n`; installed unit differed from the repository only by the `ExecCondition` line |
| 04:21 | installer `--rollback` | `{"status":"rolled_back","scheduler_unchanged":true}` |
| 04:21 | installer `--install` | `{"status":"installed_stopped","scheduler_unchanged":true}`; installed unit identical to the repository's, no `ExecCondition`; `refresh.before` unchanged |
| 04:22:00 | `systemctl --user start nhms-scheduler-file-provider-refresh.service` (attempt 1) while pass started 04:03:17 was running | wrapper logged a wait line every minute |
| 05:52:03 | attempt 1 ended | `refusing to refresh: nhms-compute-scheduler.service is still activating after waiting 5400 s (bound 5400 s); nothing was refreshed`; exit 3; unit `failed`; `latest.json` untouched |
| 05:52 | `reset-failed`; installer `--enable` with the 02:40 receipt | refused, `{"reason": "emergency_record_invalid", "status": "failed"}` (the receipt's provider checksums no longer matched the disk, as the runbook predicts); nothing changed |
| 05:52:48 | `systemctl --user start` of the refresh (attempt 2), same pass still running | wrapper waits |
| 06:09:32 | scheduler pass ended (ran 04:03:17 - 06:09:32, 126 min) | |
| 06:09:35 | refresh run `refresh_20261008T060935Z_45d269dafdd3` started | |
| 06:09:37 - 06:18:39 | observer, every 5 s, 109 samples | scheduler service `inactive`, `list-jobs`: refresh `start running`, scheduler `start waiting` |
| 06:18:41 | refresh finished, outcome `published`; scheduler pass started in the same second | |
| 06:19 | installer `--enable` | `{"status":"enabled_active","scheduler_unchanged":true}`; timer enabled/active, next elapse 2026-10-09 10:35:19 CST |
| 06:19:36 | health probe started by hand | verdict `ok`; no failed user unit |

## What this shows and what it does not

- Shown: the wrapper waits on `activating`; a queued scheduler start job reads `inactive` and stays queued
  behind the refresh job until it ends; the refresh publishes; the bound refusal (exit 3, nothing refreshed).
- Not shown: a timer-fired refresh (both starts were by hand through the same unit).
- Side effects of the cycle: the refresh timer was disarmed from 04:21 to 06:19; the hourly timer-health probe
  reported `probe_failed` at 05:04 and 06:01 in that window and `ok` afterwards.
- The observer has a gap from 04:46:53 to 05:14:48 (its loop was too short); the journal shows no state
  change of either unit in that gap. One `ssh` command of the operator killed its own shell with `pkill -f`
  at 05:52; no other process was affected and the command was repeated.
- Finding: a single pass ran 126 minutes. With the 5400 s bound a daily refresh that meets such a pass fails
  for that day (the old, broken guard ran beside the pass instead).
