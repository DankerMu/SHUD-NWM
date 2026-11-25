# Slurm Backlog Alert Runbook

## Preconditions

- Confirm the alert comes from `nhms-monitor` and is one of:
  `slurm_queue_backlog` (payload: `alert`, `severity`, `observed` = active job count, `threshold`, `runbook`), or
  `slurm_monitor_unavailable` (the monitor could not read the queue; `observed` = its error, no `threshold`).
- The payload names no partition, account or run.
- Use a host with Slurm CLI access before running live inspection.

## Commands

```bash
squeue -u "$USER" -o '%i|%P|%j|%u|%T|%M|%D|%R'
sinfo -o '%P|%a|%l|%D|%t|%N'
```

## Recovery Steps

1. Inspect partition availability, fairshare, and array limits.
2. Cancel stale controlled-failure jobs only after preserving their logs.

## Residual Risks

Queue recovery does not prove solver correctness.
