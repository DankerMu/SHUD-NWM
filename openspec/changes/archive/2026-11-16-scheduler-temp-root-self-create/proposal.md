# Scheduler re-creates a cleared temp_root instead of blocking every pass

## Why

Issue #3061. From 2026-10-15 every node-22 scheduler pass was
`preflight_blocked` on `SCHEDULER_ROOT_TEMP_ROOT_NOT_FOUND`: the configured
`NHMS_SCHEDULER_TEMP_ROOT=/scratch/frd_muziyao/nhms-prod/tmp` had been deleted,
and `services/orchestrator/scheduler_runtime_roots.py` treated every runtime root
except `published_artifact_root` as `must_exist`. A temp directory is disposable
by definition; clearing it is a normal operations event and must not stop
production compute for three weeks without self-healing.

## What Changes

- `temp_root` joins `published_artifact_root` on the runtime-root preflight's
  `allow_create` arm: a missing temp_root is admitted when its parent is an
  existing, writable, non-symlink directory and the path is inside the approved
  roots. Every other refusal (symlink, out-of-approved-root, unsafe path,
  parent missing/unwritable) keeps its existing blocker code.
- The preflight stays a pure check. A scheduler pass, right after its
  runtime-root preflight and before the `blocked` branch, creates the admitted
  temp_root: parent pinned with `open_directory_no_follow`, only the final
  component created with mode 0700. The check records `created`; a creation
  failure flips the verdict to `blocked` with `SCHEDULER_ROOT_TEMP_ROOT_<REASON>`.
- Bounded (size-pressure) evidence keeps `checks.temp_root.created`.
- `runtime_root` stays `must_exist`: it holds the persistent compute-api log
  (`infra/systemd/node22/nhms-compute-api.service`) and is the Slurm
  compute-node-visible run workspace checked by the Slurm preflight, not
  per-run scratch.

## Impact

- Code: `services/orchestrator/scheduler_runtime_roots.py`,
  `services/orchestrator/scheduler_temp_root.py` (new),
  `services/orchestrator/scheduler_runtime.py`,
  `services/orchestrator/scheduler_evidence_payload.py`.
- Spec: `orchestrator-structural-burndown` runtime-root absence scenarios.
- Docs: `docs/runbooks/two-node-production-e2e-plan.md`.
- Deployment: node-22 picks this up in the post-catch-up node-22 sync window.
