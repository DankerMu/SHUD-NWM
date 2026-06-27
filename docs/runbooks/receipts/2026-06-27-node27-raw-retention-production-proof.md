# Node-27 Raw Retention Production Proof

Captured: 2026-06-27

Scope: `migrate-downloads-to-node27-retire-node22-db` task 5.3.

## Summary

Node-27 raw retention has been upgraded from dry-run-capable mode to
production execute-only mode. The active runtime no longer exposes
`NODE27_RAW_RETENTION_DRY_RUN`, and the CLI no longer accepts `--dry-run`.

## Runtime Evidence

```text
repo=/home/nwm/NWM
head=9c1625ee
env=/home/nwm/NWM/infra/env/node27-raw-retention.env
env_mode=600
NODE27_RAW_RETENTION_DRY_RUN=absent
--dry-run CLI option=absent
timer=nhms-node27-raw-retention.timer
timer_status=active
service=nhms-node27-raw-retention.service
service_result=success
```

## Live Summary

```text
summary=/home/nwm/node27-raw-retention-logs/raw-retention-20260627T135208Z.json
schema_version=nhms.node27_raw_retention.production.v1
execution_mode=production_execute
retention_days=14
cutoff=2026-06-13T13:52:08Z
counts.planned=0
counts.deleted=0
counts.skipped=2
counts.failed=0
freed_bytes=0
skipped=raw/IFS/2026062612 within_retention_window
skipped=raw/gfs/2026062612 within_retention_window
```

## Verdict

The node-27 raw retention service is now production execute-only. No data was
deleted in the first production-mode run because current raw cycles are still
inside the 14-day retention window.
