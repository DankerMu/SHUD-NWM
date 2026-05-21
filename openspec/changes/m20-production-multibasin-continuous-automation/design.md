## Context

The repository already has a service-oriented orchestrator, real/mock Slurm gateway, production-closure validation lanes, Basins model registry, and worker implementations for GFS/IFS, canonical conversion, forcing, SHUD runtime, output parsing, frequency, and publishing. PR #190 adds a qhh-specific standard reproduction path and documents live evidence for GFS/IFS 00Z and 06Z. The next production step is to convert that proof into generic backend automation for all active registered Basins/SHUD model instances.

## Decisions

### Scheduler Boundary

Continuous automation belongs in the backend orchestration layer, not in a basin-specific script. The service scheduler SHALL discover source cycles and runnable model instances, create deterministic work candidates, and submit work through orchestrator/Slurm gateway contracts. `scripts/run_qhh_continuous.py` can remain a diagnostic fallback but MUST NOT be the production scheduler dependency.

### Candidate Identity

The canonical candidate identity is:

```text
{source_id}:{cycle_time_utc}:{model_id}:{scenario_id}
```

Run ids and forcing ids continue to use existing deterministic conventions:

```text
fcst_{source_lower}_{YYYYMMDDHH}_{model_id}
forc_{source_lower}_{YYYYMMDDHH}_{model_id}
```

Where two model instances share a `model_id` conflict is already invalid registry state; the scheduler must reject duplicate active model identities rather than generate ad hoc suffixes.

### Source Scope

Initial production scope is GFS and IFS. GFS and IFS may have different forecast horizons and availability lag. Unavailable IFS cycles are first-class `unavailable`/`blocked` evidence, not synthetic success and not silent skip.

### Execution Model

Heavy execution defaults to Slurm. The scheduler may submit shared source-level stages once per source/cycle, then array stages per model where the existing orchestrator supports it:

```text
download -> canonical -> forcing[] -> forecast[] -> parse[] -> frequency[] -> publish
```

Frequency is array-capable per model. Display/tile publication remains a cycle-level publish stage unless a later change defines a per-model publish contract. For smaller initial implementation, separate per-model jobs are acceptable only when the evidence records the non-array mode and does not regress the final array-capable contract.

### Database and Object Store Preflight

Slurm execution requires a compute-node reachable `DATABASE_URL` and object-store/workspace roots. Localhost database URLs are rejected for Slurm mode. Runtime artifacts must remain under project-configured workspace/object-store roots or production object storage, never system disk defaults.

### State and Idempotency

State is persisted in database tables and events, not only filesystem JSON. `unavailable` and `blocked` are scheduler reason codes stored in pipeline/event details unless a migration explicitly extends the relevant database enum; they are not written directly into `met.cycle_status` values that lack those enum members. Repeated scheduler scans must:

- skip terminal success candidates;
- detect active submitted/running Slurm jobs;
- resume after downstream parse/publish failures without re-running a successful SHUD execution when durable output exists;
- retry failed/unavailable candidates according to configured policy;
- preserve partial success for multi-basin cycles using existing M3 reduced-manifest and `_partial` aggregate-state semantics;
- treat `hydro.hydro_run` `succeeded`, `parsed`, `frequency_done`, and `published` as durable successful stage states according to the downstream retry point.

### Model-Run Assembly

Production model-run assembly reuses Basins registry and model package data. Basin-specific assumptions like qhh forcing station seeding, SHUD output-river identity, and display product handling must become reusable contracts driven by model metadata or package artifacts. Missing optional products must become explicit unavailable/quality states rather than fabricated data.

### Evidence and Operations

Each scheduler pass emits structured evidence covering candidates, selected/skipped reasons, source availability, submitted job ids, array task summaries, Slurm accounting, resource metrics, forcing station counts, SHUD output row counts, parse status, frequency/display status, and residual blockers. Fast validation uses deterministic fixtures and unit/integration tests; live multi-cycle reruns remain opt-in.

Resource metrics that do not fit existing `ops.pipeline_job` columns are recorded in `ops.pipeline_event.details` and scheduler evidence artifacts unless an implementation issue adds a migration. Evidence must label deterministic fixture runs separately from opt-in live executions and must not set final production readiness to true without accepted live receipts.

## Risks and Mitigations

- **Risk: qhh script logic diverges from production orchestration.** Mitigation: encode the reusable behavior in orchestrator/workers and keep qhh script as diagnostic evidence only.
- **Risk: duplicate cycle scans submit duplicate jobs.** Mitigation: enforce candidate identity and active-state locks in DB before Slurm submission.
- **Risk: Slurm jobs cannot write back to local PG.** Mitigation: reject localhost DB URLs in Slurm mode and record preflight blockers.
- **Risk: array partial failures hide basin failures.** Mitigation: persist task-level results and aggregate cycle state as partial, not success.
- **Risk: fast CI overclaims production readiness.** Mitigation: deterministic tests verify contracts; full live GFS/IFS/SHUD multi-cycle runs are opt-in evidence.

## Open Questions

- Whether production scheduler should be a long-running API-managed service, a cron/systemd command, or both. The implementation issues should support a command first and expose service/API hooks where existing patterns make it cheap.
- Whether frequency/display publication should be a separate array stage or merged into parse for the first production implementation. The contract requires evidence either way.
