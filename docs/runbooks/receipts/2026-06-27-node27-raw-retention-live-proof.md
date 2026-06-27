# Node-27 Raw Retention Live Proof

Captured: 2026-06-27

Scope: `migrate-downloads-to-node27-retire-node22-db` task 5.2.

## Summary

Node-27 raw NFS source-data retention is installed as a user systemd timer for
`nwm`. The runner is deployed on `master` at `24505db7`, with a 14-day retention
window, explicit dry-run support, and JSON evidence under
`/home/nwm/node27-raw-retention-logs`.

## Installed Runtime

```text
repo=/home/nwm/NWM
head=24505db7
env=/home/nwm/NWM/infra/env/node27-raw-retention.env
env_mode=600
timer=nhms-node27-raw-retention.timer
timer_status=active
service=nhms-node27-raw-retention.service
service_result=success
service_active=inactive
```

Effective retention env:

```text
NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
NODE27_RAW_RETENTION_LOG_ROOT=/home/nwm/node27-raw-retention-logs
NODE27_RAW_RETENTION_LOCK_PATH=/tmp/node27-raw-retention.lock
NODE27_RAW_RETENTION_SOURCES=GFS,IFS
NODE27_RAW_RETENTION_DAYS=14
NODE27_RAW_RETENTION_DRY_RUN=false
```

Timer schedule:

```text
OnCalendar=*-*-* 03:35:00 UTC
next=Sun 2026-06-28 11:35:00 CST
```

## Dry-Run Evidence

```text
summary=/home/nwm/node27-raw-retention-logs/raw-retention-20260627T133734Z.json
schema_version=nhms.node27_raw_retention.v1
dry_run=true
retention_days=14
cutoff=2026-06-13T13:37:34Z
counts.planned=0
counts.deleted=0
counts.skipped=2
counts.failed=0
skipped=raw/IFS/2026062612 within_retention_window
skipped=raw/gfs/2026062612 within_retention_window
```

## Live Execute Evidence

```text
summary=/home/nwm/node27-raw-retention-logs/raw-retention-20260627T133735Z.json
schema_version=nhms.node27_raw_retention.v1
dry_run=false
retention_days=14
cutoff=2026-06-13T13:37:35Z
counts.planned=0
counts.deleted=0
counts.skipped=2
counts.failed=0
freed_bytes=0
skipped=raw/IFS/2026062612 within_retention_window
skipped=raw/gfs/2026062612 within_retention_window
```

## Verdict

The node-27 raw retention timer is live, safe-scoped to
`/home/ghdc/nwm/object-store/raw/<source>/<YYYYMMDDHH>`, and leaves auditable
JSON evidence for both dry-run and execute runs. The first execute run deleted
nothing because all current raw cycles were still inside the configured
retention window.
