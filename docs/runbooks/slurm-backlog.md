# Slurm Backlog Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_slurm_queue_backlog_jobs`, partition, account, and run ID.
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

Queue recovery does not prove solver correctness. Final readiness still requires accepted Slurm evidence and live alert receipts.
