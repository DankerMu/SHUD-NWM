# Node-22 Slurm Gateway Submission Preflight Runbook

Scope: the pre-submit gate the production scheduler runs before handing a SHUD
run to the node-22 Slurm gateway / host submission path (M23-7, issue #258).
When any check fails the scheduler records a typed blocker and does **not**
submit, mark an active job, or mark a hydro success (`never-fake-success`). When
the gateway is genuinely healthy and reachable, the gateway check adds no blocker
(`never-break-userspace`).

## Preconditions

- `slurm_execution_enabled` is true (`SLURM_EXECUTION_ENABLED` /
  `NHMS_PRODUCTION_SLURM_ENABLED`). Otherwise preflight reports `not_required`.
- `SLURM_GATEWAY_URL` points at the real node-22 gateway (default
  `http://localhost:8000` is the co-located dev/mock convention only).
- `SLURM_GATEWAY_BACKEND` selects `mock` (dev) or `real`/`slurm` (production).

## What preflight checks (`_slurm_preflight` -> `gateway`)

1. **Self-reference (deterministic, no network).** With a `real`/`slurm`
   backend, a gateway URL whose host is loopback / `0.0.0.0` / `localhost` AND
   whose port equals this service's own control-API port
   (`NHMS_SERVICE_PORT`, default `8000`) is rejected as
   `SLURM_GATEWAY_SELF_REFERENCE` — the "gateway" would loop back to the
   orchestrator's own API. The mock co-located default is intentionally allowed.
2. **Health / capability (bounded, fail-safe).** Probes gateway health
   (`sinfo --version` for the real backend), submit capability, and accounting
   availability. Any of: health failure, missing Slurm CLI, or accounting
   unavailable -> `SLURM_GATEWAY_UNAVAILABLE`. The probe is bounded and never
   raises: a probe that cannot determine state fails **BLOCKED**, never a false
   PASS. On a node-22 compute container with no Slurm CLI this BLOCKED state is a
   real, expected terminal outcome — not a bug.

Adjacent pre-submit checks (same gate): allowed sbatch templates
(`infra/sbatch` allowlist), storage roots (workspace / object store / **log
root** / runtime), `slurm_env` policy, and the SHUD executable (issue #257).
Account / partition / resource policy is carried via the resource profile and
sbatch templates.

## Evidence

`slurm_preflight.checks.gateway` records `mode` (mock/real), endpoint
`host:port`, `self_reference`, `healthy`, `submit_capable`, and
`accounting_available`. The whole payload is run through `redact_payload` and
the gateway URL's `user:pass@` userinfo is stripped — **no credentials are ever
written to evidence**.

## Receipts (on successful submit)

- `ops.pipeline_job`: `slurm_job_id`, `array_task_id` (when the gateway reports
  one), `status`, `exit_code`, `log_uri`, submit/started/finished timestamps.
- `ops.pipeline_event.details.slurm`: per-task `array_task_id`, state,
  `log_uri`, `accounting`, and `resource_metrics` when available.

## Recovery

1. `SLURM_GATEWAY_SELF_REFERENCE`: point `SLURM_GATEWAY_URL` at the real
   gateway host, or run a `mock` backend for dev.
2. `SLURM_GATEWAY_UNAVAILABLE`: from a Slurm-capable host check
   `sinfo --version` and `sacct`; confirm the gateway service is up and the
   compute context has the Slurm CLI. Re-scan once the gateway is healthy.

## Residual Risks

A green gateway preflight proves reachability and capability, not solver
correctness. Final readiness still requires accepted Slurm accounting receipts
and successful SHUD output (issues #257/#259).
