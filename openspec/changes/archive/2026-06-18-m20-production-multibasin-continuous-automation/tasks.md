## 1. Scheduler Discovery and Planning

- [ ] 1.1 Add a production scheduler entrypoint that supports one-shot, continuous, dry-run, source selection, cycle lookback/lag, and max-cycles-per-source.
- [ ] 1.2 Implement scheduler locking/lease behavior for continuous and cron/systemd-style execution so concurrent passes cannot submit duplicate candidates.
- [ ] 1.3 Implement active registered basin/model discovery from the registry with complete model, basin, river network, package URI, and resource profile metadata; default to all active runnable models and record explicit operator filters.
- [ ] 1.4 Implement GFS/IFS cycle candidate discovery with source-specific availability, horizon, lag, and unavailable/block reason evidence without writing unsupported enum states.
- [ ] 1.5 Add deterministic candidate identity, run id, forcing version id, duplicate active model rejection, lock contention, and dry-run no-mutation tests.
- [ ] 1.6 For issue #192, include regression evidence for every row in the Issue #192 Invariant Matrix: all-active discovery, explicit filter evidence, lock contention, dry-run no-mutation, unavailable IFS reason storage, and duplicate active model rejection.

## 2. Full-Chain Model-Run Assembly

- [ ] 2.1 Define reusable model-run assembly contracts for Basins package/manifest resolution, forcing station metadata, SHUD project mode inputs, SHUD output-river identity, and output URI reuse.
- [ ] 2.2 Wire candidate orchestration through existing workers for download, canonical conversion, forcing production, native SHUD runtime, and output parser using deterministic fixtures.
- [ ] 2.3 Implement frequency and cycle-level display/tile publication handoff with explicit unavailable/quality states for missing frequency curves, warning thresholds, station forcing, or optional weather products.
- [ ] 2.4 Add focused qhh fixture regression proving the production scheduler can plan and execute the same standard chain shape without invoking qhh-specific continuous scripts or requiring a live full-chain rerun.
- [ ] 2.5 For issue #193, include regression evidence for every row in the Issue #193 Invariant Matrix:
  - qhh active model candidate -> generic production chain shape uses registry/package metadata and does not invoke qhh-specific continuous scripts.
  - candidate model/package/forcing identity -> manifest index, runtime manifest, hydro run, parser input, frequency handoff, publish evidence all carry the same run/model/source/cycle identifiers.
  - missing frequency curves or warning thresholds -> explicit quality/unavailable state and residual blocker; no fabricated return periods or warning values.
  - missing station forcing or optional weather/display product -> stable unavailable/quality evidence while successful durable outputs remain reusable where valid.
  - partial model success in a cycle -> reduced downstream manifests and cycle-level publish over successful basins only.
  - unchanged non-qhh model fixture -> existing orchestrator and worker tests still pass with the same manifest schema and status contracts.
- [ ] 2.6 Required verification for #193: `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_shud_runtime.py tests/test_output_parser.py tests/test_flood_frequency.py tests/test_production_slurm_validation.py` plus `uv run ruff check .` and `openspec validate m20-production-multibasin-continuous-automation --strict --no-interactive`.

## 3. Slurm and Array Execution

- [ ] 3.1 Add Slurm preflight for compute-node reachable database, workspace/object-store roots, runtime dependency paths, log roots, and storage visibility/safety.
- [ ] 3.2 Integrate scheduler submissions with the real/mock Slurm gateway and existing sbatch template allowlist.
- [ ] 3.3 Support array-capable forcing, forecast, parse, and frequency model stages with task-level manifest indexes; keep display publish cycle-level unless a new publish contract is added.
- [ ] 3.4 Persist Slurm job id, array task id, state, exit code, and log URI in existing pipeline fields, and persist elapsed time, MaxRSS, and resource metrics in pipeline event details or scheduler evidence artifacts unless a migration adds dedicated columns.
- [ ] 3.5 Add tests for Slurm preflight rejection, safe export/env handling, array partial success, and accounting evidence.
- [ ] 3.6 For issue #194, include regression evidence for every row in the Issue #194 Invariant Matrix:
  - Slurm enabled with missing or localhost `DATABASE_URL` -> preflight blocker before Slurm submit, with no active pipeline submission.
  - Slurm enabled with missing or out-of-root workspace/object-store/log/runtime roots -> storage preflight blocker before Slurm submit.
  - Allowed Slurm template/resource/env -> gateway submit receives allowlisted template and shell-safe bounded env without secret leakage.
  - Array forcing/forecast/parse/frequency partial failure -> task-level state persists, downstream manifest is reduced to successful eligible model tasks, and aggregate status uses `_partial`.
  - Slurm accounting available -> job id, array task id, state, exit code, log URI, elapsed, MaxRSS/resource metrics appear in pipeline event details or scheduler evidence.
  - Slurm accounting unavailable/malformed -> stable evidence gap/blocker without fabricated metrics.
  - Repeated scan with active Slurm job -> no duplicate submission.
  - Cancellation -> Slurm cancel contract is called and no replacement work is submitted in the same pass.
  - Unchanged non-Slurm/mock path -> existing dry-run, deterministic fixture, and worker/orchestrator tests still pass.
- [ ] 3.7 Required verification for #194: `uv run pytest -q tests/test_production_slurm_validation.py tests/test_slurm_array_contract.py tests/test_production_scheduler.py tests/test_orchestration_chain.py` plus any Slurm gateway tests touched by the implementation, `uv run ruff check .`, and `openspec validate m20-production-multibasin-continuous-automation --strict --no-interactive`.

## 4. State, Idempotency, and Retry

Issue scope note: section 4 is implemented by #195. #194 may touch active Slurm skip or cancellation evidence only where required by section 3's Slurm submission invariant; full retry/cancellation policy belongs to #195.

- [ ] 4.1 Persist candidate/stage state through `ops.pipeline_job`, `ops.pipeline_event`, `met.forecast_cycle`, `met.forcing_version`, and `hydro.hydro_run` where applicable.
- [ ] 4.2 Implement skip behavior for terminal successful candidates, including `succeeded`, `parsed`, `frequency_done`, and `published` hydro runs, and active submitted/running Slurm jobs.
- [ ] 4.3 Implement resumable retries after downstream parse/display failures without rerunning successful native SHUD output by default.
- [ ] 4.4 Implement retry policy distinctions for source unavailable, adapter failure, forcing failure, SHUD failure, parse failure, publish/frequency failure, transient Slurm/runtime failure, non-transient permanent failure, manual retry, and cancellation.
- [ ] 4.5 Add tests for repeated scheduler scans, active-job skip, terminal skip, source unavailable retry, parse-after-SHUD retry, transient task retry, permanent failure guard, and cancellation.
- [ ] 4.6 For issue #195, include regression evidence for every row in the Issue #195 Invariant Matrix:
  - Repeated scan with terminal pipeline/hydro success (`succeeded`, `parsed`, `frequency_done`, `published`) -> skip with terminal reason and no Slurm/orchestrator submission.
  - Repeated scan with active submitted/running Slurm job -> active skip/resume evidence and no duplicate submission.
  - Parse/display failure after durable SHUD output -> retry starts at parse/frequency/publish point and does not rerun native SHUD by default.
  - Source unavailable candidate -> retryable unavailable evidence distinct from model/runtime failure and no unsupported DB enum state.
  - Transient Slurm/runtime/task failure within retry limit -> retry scoped to failed candidate/task/stage while successful siblings/durable outputs are reused.
  - Non-transient, malformed input, policy-blocked, or retry-limit-exhausted failure -> permanent failure evidence and automatic retry stops.
  - Manual retry marker for permanent/blocked candidate -> explicit retry allowed, attempt evidence increments, and prior failure reason remains auditable.
  - Cancellation request for active job -> Slurm cancel contract called, proof or gap recorded, local state preserved on unproven cancellation, and no replacement work submitted in same pass.
  - Unchanged non-Slurm/mock/API consumers -> existing dry-run, mock gateway, retry route, cancel route, and monitoring tests keep passing.
- [ ] 4.7 Required verification for #195: `uv run pytest -q tests/test_production_scheduler.py tests/test_retry_cancel_consistency.py tests/test_orchestration_chain.py tests/test_job_array.py tests/test_api.py tests/test_gateway.py` plus any worker/orchestrator tests touched by the implementation, `uv run ruff check .`, and `openspec validate m20-production-multibasin-continuous-automation --strict --no-interactive`.

## 5. Evidence, Operations, and Validation

Issue scope note: section 5 is implemented by #196. #194 should emit only the Slurm preflight/submission/accounting evidence needed for section 3; operator docs, readiness ingestion, and deterministic-vs-live validation are completed by #196.

- [ ] 5.1 Emit scheduler pass and model-run evidence with execution mode, candidate counts, selected model filters, skip/block reasons, artifact paths, forcing station counts, parsed row counts, segment counts, display states, quality flags, Slurm accounting, resource metrics, and residual blockers.
- [ ] 5.2 Add dry-run output and operator-facing command documentation/runbook for production scheduler use, including explicit no-download/no-Slurm/no-SHUD/no-hydro-met-mutation behavior.
- [ ] 5.3 Extend production validation/readiness evidence to ingest the scheduler evidence without requiring full live multi-cycle reruns.
- [ ] 5.4 Preserve deterministic-vs-live truth table semantics so fast scheduler evidence cannot set final production readiness true without accepted live receipts.
- [ ] 5.5 Update `progress.md`, validation docs, and qhh continuous runbook to distinguish diagnostic qhh scripts from production scheduler automation.
- [ ] 5.6 For issue #196, include regression evidence for every row in the Issue #196 Invariant Matrix:
  - scheduler dry-run pass -> evidence reports selected candidates, filters, skip/block reasons, artifact path, and no download/Slurm/SHUD/hydro-met mutation proof.
  - scheduler submitted/partial/blocked pass -> pass and model-run evidence include execution mode, counts, source/cycle/model ids, artifact refs, stage/resource/accounting metrics where available, and residual blockers without leaking secrets.
  - deterministic scheduler evidence consumed by readiness validation -> readiness item is deterministic/non-final and `final_production_readiness_claimed=false` when live receipts are absent.
  - scheduler evidence with accepted live receipt binding -> readiness records receipt refs/checksum as live proof input only when schema, run id, target environment, producer artifact/ref, and execution mode match the M19 live proof contract.
  - malformed, oversized, stale, or identity-mismatched scheduler evidence -> stable blocked/release_blocked evidence with redacted reason and no final readiness claim.
  - qhh continuous script evidence/runbook -> remains diagnostic and must not be described as the production scheduler dependency or as final readiness proof.
  - unchanged M19 readiness producer summaries -> existing Slurm/object-store/source/E2E/MVT deterministic summaries still ingest with the same final readiness truth table.
  - unchanged monitoring/API/orchestrator consumers -> existing scheduler/orchestrator/retry/cancel tests keep passing with the same evidence/status contracts.
- [ ] 5.7 Required verification for #196: `uv run pytest -q tests/test_production_scheduler.py tests/test_production_readiness_validation.py tests/test_orchestration_chain.py tests/test_production_slurm_validation.py tests/test_retry_cancel_consistency.py tests/test_api.py tests/test_monitoring_api.py` plus any worker/orchestrator tests touched by the implementation, `uv run ruff check .`, `openspec validate m20-production-multibasin-continuous-automation --strict --no-interactive`, and a fast readiness validation command that writes scheduler evidence without claiming final production readiness.
