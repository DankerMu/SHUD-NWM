# Slurm Backlog Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_slurm_queue_backlog_jobs`, partition, account, and run ID.
- Use a host with Slurm CLI access before running live inspection.

## Commands

```bash
squeue -u "$USER" -o '%i|%P|%j|%u|%T|%M|%D|%R'
sinfo -o '%P|%a|%l|%D|%t|%N'
uv run nhms-production validate-slurm --evidence-root artifacts/production-closure --run-id slurm-backlog-check --fake-slurm
```

## Expected Evidence

- `slurm/slurm_accounting.json` or live CLI output records queue state and partition health.
- `ops/monitoring_alerts.json` records severity, threshold, and operator action.

## Recovery Steps

1. Inspect partition availability, fairshare, and array limits.
2. Cancel stale controlled-failure jobs only after preserving their logs.
3. Re-run the Slurm closure lane when capacity is restored.

## Residual Risks

Queue recovery does not prove solver correctness. Final readiness still requires accepted Slurm evidence and live alert receipts.
