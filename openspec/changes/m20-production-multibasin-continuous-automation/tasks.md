## 1. Scheduler Discovery and Planning

- [ ] 1.1 Add a production scheduler entrypoint that supports one-shot, continuous, dry-run, source selection, cycle lookback/lag, and max-cycles-per-source.
- [ ] 1.2 Implement scheduler locking/lease behavior for continuous and cron/systemd-style execution so concurrent passes cannot submit duplicate candidates.
- [ ] 1.3 Implement active registered basin/model discovery from the registry with complete model, basin, river network, package URI, and resource profile metadata; default to all active runnable models and record explicit operator filters.
- [ ] 1.4 Implement GFS/IFS cycle candidate discovery with source-specific availability, horizon, lag, and unavailable/block reason evidence without writing unsupported enum states.
- [ ] 1.5 Add deterministic candidate identity, run id, forcing version id, duplicate active model rejection, lock contention, and dry-run no-mutation tests.

## 2. Full-Chain Model-Run Assembly

- [ ] 2.1 Define reusable model-run assembly contracts for Basins package/manifest resolution, forcing station metadata, SHUD project mode inputs, SHUD output-river identity, and output URI reuse.
- [ ] 2.2 Wire candidate orchestration through existing workers for download, canonical conversion, forcing production, native SHUD runtime, and output parser using deterministic fixtures.
- [ ] 2.3 Implement frequency and cycle-level display/tile publication handoff with explicit unavailable/quality states for missing frequency curves, warning thresholds, station forcing, or optional weather products.
- [ ] 2.4 Add focused qhh fixture regression proving the production scheduler can plan and execute the same standard chain shape without invoking qhh-specific continuous scripts or requiring a live full-chain rerun.

## 3. Slurm and Array Execution

- [ ] 3.1 Add Slurm preflight for compute-node reachable database, workspace/object-store roots, runtime dependency paths, log roots, and storage visibility/safety.
- [ ] 3.2 Integrate scheduler submissions with the real/mock Slurm gateway and existing sbatch template allowlist.
- [ ] 3.3 Support array-capable forcing, forecast, parse, and frequency model stages with task-level manifest indexes; keep display publish cycle-level unless a new publish contract is added.
- [ ] 3.4 Persist Slurm job id, array task id, state, exit code, and log URI in existing pipeline fields, and persist elapsed time, MaxRSS, and resource metrics in pipeline event details or scheduler evidence artifacts unless a migration adds dedicated columns.
- [ ] 3.5 Add tests for Slurm preflight rejection, safe export/env handling, array partial success, and accounting evidence.

## 4. State, Idempotency, and Retry

- [ ] 4.1 Persist candidate/stage state through `ops.pipeline_job`, `ops.pipeline_event`, `met.forecast_cycle`, `met.forcing_version`, and `hydro.hydro_run` where applicable.
- [ ] 4.2 Implement skip behavior for terminal successful candidates, including `succeeded`, `parsed`, `frequency_done`, and `published` hydro runs, and active submitted/running Slurm jobs.
- [ ] 4.3 Implement resumable retries after downstream parse/display failures without rerunning successful native SHUD output by default.
- [ ] 4.4 Implement retry policy distinctions for source unavailable, adapter failure, forcing failure, SHUD failure, parse failure, publish/frequency failure, transient Slurm/runtime failure, non-transient permanent failure, manual retry, and cancellation.
- [ ] 4.5 Add tests for repeated scheduler scans, active-job skip, terminal skip, source unavailable retry, parse-after-SHUD retry, transient task retry, permanent failure guard, and cancellation.

## 5. Evidence, Operations, and Validation

- [ ] 5.1 Emit scheduler pass and model-run evidence with execution mode, candidate counts, selected model filters, skip/block reasons, artifact paths, forcing station counts, parsed row counts, segment counts, display states, quality flags, Slurm accounting, resource metrics, and residual blockers.
- [ ] 5.2 Add dry-run output and operator-facing command documentation/runbook for production scheduler use, including explicit no-download/no-Slurm/no-SHUD/no-hydro-met-mutation behavior.
- [ ] 5.3 Extend production validation/readiness evidence to ingest the scheduler evidence without requiring full live multi-cycle reruns.
- [ ] 5.4 Preserve deterministic-vs-live truth table semantics so fast scheduler evidence cannot set final production readiness true without accepted live receipts.
- [ ] 5.5 Update `progress.md`, validation docs, and qhh continuous runbook to distinguish diagnostic qhh scripts from production scheduler automation.
- [ ] 5.6 Run OpenSpec strict validation, ruff, focused scheduler/orchestrator/Slurm tests, worker regression tests touched by the change, and fast evidence validation.
