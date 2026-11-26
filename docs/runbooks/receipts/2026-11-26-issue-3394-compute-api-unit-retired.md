# Compute API unit retired on the compute control host (issue #3394 slice 2; 2026-11-26)

Operator receipt for one edit of the production user's systemd directory on `w3`, the compute role's
control host: the installed `nhms-compute-api.service` was moved aside. The unit is retired by #3394;
its tracked copy and its `MANIFEST.json` entry are removed by the same pull request as this receipt.

## State before (read-only, 2026-11-26 16:37Z)

| Fact | Value |
|---|---|
| installed file | `nhms-compute-api.service`, mode `0600`, 858 bytes, mtime 2026-06-12 |
| sha256 | `c6dff9aa650bb0741401e1da0cdd0120cc66a403373d8803b214d322b7469ab7` (equal to the tracked copy) |
| `systemctl --user is-enabled` / `is-active` | `disabled` / `inactive`; no activation since the user manager started |
| listeners on 8001 | none |
| `uvicorn apps.api.main` processes | none |
| other installed units naming it | none |

## What was done (2026-11-26 17:34:47Z to 17:34:49Z)

The scheduler service was `inactive` (no pass running); no timer was stopped.

1. Drift probe from the production checkout (`948569fc5`, manifest with 25 entries): exit 0, receipt
   below.
2. `mv -n` of the installed file into `retired-20261126/` inside the systemd user directory (the probe
   scans the top level and one level of `nhms-*.service.d/`, so the directory is not read; sha256 of the
   moved file unchanged).
3. `systemctl --user daemon-reload`: exit 0. `systemctl --user status nhms-compute-api.service`:
   `Unit nhms-compute-api.service could not be found.` (`LoadState=not-found`).
4. Drift probe with the manifest of the pull request head (`c0524d00d`, 24 entries), run with the
   production interpreter from an export of that commit (`git archive` of
   `scripts/node22_unit_drift_check.py` and `infra/systemd/`, unpacked under `/tmp` and removed
   afterwards; the probe script is unchanged since `948569fc5`): exit 0, receipt below.

After the edit: `nhms-compute-scheduler.timer`, `nhms-slurm-gateway.service` and
`nhms-node22-scheduler-stall-health.timer` `active`; the gateway's `/api/v1/slurm/health` answered 200;
no failed user unit.

Until the production checkout is fast-forwarded to the merge, the probe run from it reports this one
entry `missing` (exit 1; observed at 17:34:49Z: 24 `same`, 1 `missing`, `untracked` empty). No timer
runs the probe.

## Not changed

- `/scratch/frd_muziyao/nhms-prod/runtime/api/nhms-api-8001.log` (22,794 bytes, last written
  2026-11-22): the unit's old log, kept.
- `infra/env/compute.host.env`: the gateway unit loads it.

## Rollback

`mv` the file back from `retired-20261126/`, `systemctl --user daemon-reload`. The unit was disabled, so
this restores an installed, not running, unit.

## Probe receipt before

```json
{
  "schema_version": "nhms.node22.unit_drift_check.v1",
  "status": "ok",
  "exit_code": 0,
  "generated_at": "2026-11-26T17:34:47.818054Z",
  "manifest_path": "/scratch/frd_muziyao/NWM/infra/systemd/node22/MANIFEST.json",
  "manifest_sha256": "c343320bc1283a47c2774ce153737123fe6903bfd7a33f03acf46a8691adfac0",
  "systemd_user_dir": "/users/frd_muziyao/.config/systemd/user",
  "untracked": [],
  "errors": [],
  "entries": [
    {"deployed_relative_path": "nhms-compute-api.service", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-api.service"},
    {"deployed_relative_path": "nhms-compute-scheduler.service", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-scheduler.service"},
    {"deployed_relative_path": "nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf"},
    {"deployed_relative_path": "nhms-compute-scheduler.service.d/20-runtime-path.conf", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-scheduler.service.d/20-runtime-path.conf"},
    {"deployed_relative_path": "nhms-compute-scheduler.timer", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-scheduler.timer"},
    {"deployed_relative_path": "nhms-node22-refresh-timer-health.service", "status": "same", "tracked_path": "infra/systemd/nhms-node22-refresh-timer-health.service"},
    {"deployed_relative_path": "nhms-node22-refresh-timer-health.timer", "status": "same", "tracked_path": "infra/systemd/nhms-node22-refresh-timer-health.timer"},
    {"deployed_relative_path": "nhms-node22-scheduler-stall-health.service", "status": "same", "tracked_path": "infra/systemd/nhms-node22-scheduler-stall-health.service"},
    {"deployed_relative_path": "nhms-node22-scheduler-stall-health.timer", "status": "same", "tracked_path": "infra/systemd/nhms-node22-scheduler-stall-health.timer"},
    {"deployed_relative_path": "nhms-node22-unit-failure-alert@.service", "status": "same", "tracked_path": "infra/systemd/nhms-node22-unit-failure-alert@.service"},
    {"deployed_relative_path": "nhms-scheduler-evidence-retention.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-evidence-retention.service"},
    {"deployed_relative_path": "nhms-scheduler-evidence-retention.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-evidence-retention.timer"},
    {"deployed_relative_path": "nhms-scheduler-file-provider-refresh.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-file-provider-refresh.service"},
    {"deployed_relative_path": "nhms-scheduler-file-provider-refresh.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-file-provider-refresh.timer"},
    {"deployed_relative_path": "nhms-scheduler-journal-retention.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-journal-retention.service"},
    {"deployed_relative_path": "nhms-scheduler-journal-retention.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-journal-retention.timer"},
    {"deployed_relative_path": "nhms-scheduler-state-index-capacity.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-state-index-capacity.service"},
    {"deployed_relative_path": "nhms-scheduler-state-index-capacity.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-state-index-capacity.timer"},
    {"deployed_relative_path": "nhms-scheduler-state-index-prune.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-state-index-prune.service"},
    {"deployed_relative_path": "nhms-scheduler-state-index-prune.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-state-index-prune.timer"},
    {"deployed_relative_path": "nhms-slurm-gateway.service", "status": "same", "tracked_path": "infra/systemd/node22/nhms-slurm-gateway.service"},
    {"deployed_relative_path": "nhms-slurm-gateway.service.d/10-node22-live.conf", "status": "same", "tracked_path": "infra/systemd/node22/nhms-slurm-gateway.service.d/10-node22-live.conf"},
    {"deployed_relative_path": "nhms-slurm-gateway.service.d/20-runtime-path.conf", "status": "same", "tracked_path": "infra/systemd/node22/nhms-slurm-gateway.service.d/20-runtime-path.conf"},
    {"deployed_relative_path": "nhms-w3-journal-backup.service", "status": "same", "tracked_path": "infra/systemd/nhms-w3-journal-backup.service"},
    {"deployed_relative_path": "nhms-w3-journal-backup.timer", "status": "same", "tracked_path": "infra/systemd/nhms-w3-journal-backup.timer"}
  ]
}
```

## Probe receipt after

```json
{
  "schema_version": "nhms.node22.unit_drift_check.v1",
  "status": "ok",
  "exit_code": 0,
  "generated_at": "2026-11-26T17:34:48.706930Z",
  "manifest_path": "/tmp/issue3394-prhead/infra/systemd/node22/MANIFEST.json",
  "manifest_sha256": "f33f0d12bee93c22a53712a87509d5b826a34d81b58cebbced4d657c9c763c2b",
  "systemd_user_dir": "/users/frd_muziyao/.config/systemd/user",
  "untracked": [],
  "errors": [],
  "entries": [
    {"deployed_relative_path": "nhms-compute-scheduler.service", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-scheduler.service"},
    {"deployed_relative_path": "nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf"},
    {"deployed_relative_path": "nhms-compute-scheduler.service.d/20-runtime-path.conf", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-scheduler.service.d/20-runtime-path.conf"},
    {"deployed_relative_path": "nhms-compute-scheduler.timer", "status": "same", "tracked_path": "infra/systemd/node22/nhms-compute-scheduler.timer"},
    {"deployed_relative_path": "nhms-node22-refresh-timer-health.service", "status": "same", "tracked_path": "infra/systemd/nhms-node22-refresh-timer-health.service"},
    {"deployed_relative_path": "nhms-node22-refresh-timer-health.timer", "status": "same", "tracked_path": "infra/systemd/nhms-node22-refresh-timer-health.timer"},
    {"deployed_relative_path": "nhms-node22-scheduler-stall-health.service", "status": "same", "tracked_path": "infra/systemd/nhms-node22-scheduler-stall-health.service"},
    {"deployed_relative_path": "nhms-node22-scheduler-stall-health.timer", "status": "same", "tracked_path": "infra/systemd/nhms-node22-scheduler-stall-health.timer"},
    {"deployed_relative_path": "nhms-node22-unit-failure-alert@.service", "status": "same", "tracked_path": "infra/systemd/nhms-node22-unit-failure-alert@.service"},
    {"deployed_relative_path": "nhms-scheduler-evidence-retention.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-evidence-retention.service"},
    {"deployed_relative_path": "nhms-scheduler-evidence-retention.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-evidence-retention.timer"},
    {"deployed_relative_path": "nhms-scheduler-file-provider-refresh.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-file-provider-refresh.service"},
    {"deployed_relative_path": "nhms-scheduler-file-provider-refresh.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-file-provider-refresh.timer"},
    {"deployed_relative_path": "nhms-scheduler-journal-retention.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-journal-retention.service"},
    {"deployed_relative_path": "nhms-scheduler-journal-retention.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-journal-retention.timer"},
    {"deployed_relative_path": "nhms-scheduler-state-index-capacity.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-state-index-capacity.service"},
    {"deployed_relative_path": "nhms-scheduler-state-index-capacity.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-state-index-capacity.timer"},
    {"deployed_relative_path": "nhms-scheduler-state-index-prune.service", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-state-index-prune.service"},
    {"deployed_relative_path": "nhms-scheduler-state-index-prune.timer", "status": "same", "tracked_path": "infra/systemd/nhms-scheduler-state-index-prune.timer"},
    {"deployed_relative_path": "nhms-slurm-gateway.service", "status": "same", "tracked_path": "infra/systemd/node22/nhms-slurm-gateway.service"},
    {"deployed_relative_path": "nhms-slurm-gateway.service.d/10-node22-live.conf", "status": "same", "tracked_path": "infra/systemd/node22/nhms-slurm-gateway.service.d/10-node22-live.conf"},
    {"deployed_relative_path": "nhms-slurm-gateway.service.d/20-runtime-path.conf", "status": "same", "tracked_path": "infra/systemd/node22/nhms-slurm-gateway.service.d/20-runtime-path.conf"},
    {"deployed_relative_path": "nhms-w3-journal-backup.service", "status": "same", "tracked_path": "infra/systemd/nhms-w3-journal-backup.service"},
    {"deployed_relative_path": "nhms-w3-journal-backup.timer", "status": "same", "tracked_path": "infra/systemd/nhms-w3-journal-backup.timer"}
  ]
}
```
