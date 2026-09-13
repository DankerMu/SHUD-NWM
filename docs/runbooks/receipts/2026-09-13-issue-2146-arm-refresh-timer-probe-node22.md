# Receipt: arm the node-22 refresh timer health probe (#2146, PR #2289)

- Date: 2026-09-13 (UTC)
- Host: node-22, `/scratch/frd_muziyao/NWM`, user systemd (systemd 255, TZ Asia/Shanghai)
- Rules: #1831 exception. No `uv`, no DB, no Slurm. The unit runs the exact interpreter
  `/scratch/frd_muziyao/NWM/.venv/bin/python`. The refresh installer
  `install_node22_scheduler_file_provider_refresh.sh` was **not run in any mode**.

## Sync

- Pre-pull `git status --porcelain`: only `?? .nhms-work/` (gitignored work dir, untouched).
- `git pull --ff-only`: `99bd9d22` -> `7b38bcb8` (merge of PR #2289). No stash.

## Protected units before (04:39:33Z)

| unit | is-enabled | is-active |
|---|---|---|
| nhms-compute-scheduler.timer | enabled | active |
| nhms-compute-scheduler.service | static | activating (a normal compute pass) |
| nhms-scheduler-file-provider-refresh.timer | enabled | active |
| nhms-scheduler-file-provider-refresh.service | static | inactive |
| nhms-node22-refresh-timer-health.timer | not-found | inactive |
| nhms-node22-refresh-timer-health.service | not-found | inactive |

## Arming

```text
$ bash scripts/install_node22_refresh_timer_health.sh --install
{"status":"installed_stopped","protected_unchanged":true}     # exit 0
probe timer disabled/inactive, probe service static/inactive

$ bash scripts/install_node22_refresh_timer_health.sh --enable
{"status":"enabled_active","protected_unchanged":true}        # exit 0
```

`Persistent=true` with no stamp did not fire right away. The first scheduled tick came at 13:01:05 CST.

## First systemd ticks

```text
Sep 13 13:01:05 node22-refresh-timer-health: verdict=ok
Sep 13 14:03:30 node22-refresh-timer-health: verdict=ok
Result=success ExecMainStatus=0
NEXT Sun 2026-09-13 15:02:29 CST (hourly + <=5m jitter)
```

Receipt `/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/receipts/latest.json`
(dir 0700, file 0600):

```json
{"active_state": "active", "generated_at": "2026-09-13T06:03:30.676792Z",
 "inactive_enter_timestamp": "Fri 2026-08-28 00:11:18 CST",
 "last_trigger": "Sun 2026-09-13 10:23:33 CST", "manifest_age_hours": 3.6565,
 "manifest_source": "latest", "max_manifest_age_hours": 120, "max_next_dwell_hours": 36,
 "next_elapse": "Mon 2026-09-14 10:18:12 CST",
 "schema_version": "nhms.node22.refresh_timer_health.v1", "stopped_dwell_hours": 6,
 "sub_state": "waiting", "unit": "nhms-scheduler-file-provider-refresh.timer",
 "unit_file_state": "enabled", "verdict": "ok"}
```

This proves the unit, not just the script: `ExecStart`, `WorkingDirectory`, `UnsetEnvironment`,
`UMask` and the default receipt root all work under systemd.

## Protected units after (06:47:27Z)

The four protected units show the same enabled/active values as before. The refresh
service is `inactive` between ticks. The compute service is `activating` because a normal
compute pass is running.

## Rollback

`bash scripts/install_node22_refresh_timer_health.sh --rollback` disarms the probe and
reads back each unit. It refuses to report success until both are inert.
