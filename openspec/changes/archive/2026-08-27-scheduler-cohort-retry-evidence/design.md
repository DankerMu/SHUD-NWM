## Context

Issues #1562 and #1199 share one operational invariant: cohort-level retry control must remain conservative while exposing enough model-level evidence to explain its decision. Current `completed_pipeline_init_state_id_occurrences` accepts only aggregate-success masters, so a quarantined model that succeeded beside a failed basin is invisible. Current `_terminal_stage_needs_forced_resubmit` returns at the first non-qualifying basin, so a mixed cohort can resume a terminal job without saying which basin vetoed or how many siblings requested replacement.

Fixture level: **expanded**. Repair intensity: **high** because this touches an orchestrator state-machine gate, a shared returned-evidence contract, and a read-only provenance chain. #1199's upstream suggestion was `expanded` (agree); #1562 has no upstream suggestion.

A historical constraint matters: `replay_manual_retry_admission` exists only on archived commit `2094d480` in the never-merged `feat/issue-1164-six-basin-replay` branch. It is not a `master` compatibility surface and this change must not recreate it.

## Goals / Non-Goals

**Goals:**

- Arm the §8.7 breaker after a proven successful model task even when its cohort master is `partially_failed`.
- Never count the model when its own task failed or its exact projection is unavailable.
- Return one bounded, typed, candidate-bound veto record for a mixed cohort while preserving the boolean verdict.
- Keep the record visible through normal scheduler receipts and bounded candidate summarization.

**Non-Goals:**

- No whitelist/capability-registry, retry threshold, cadence, cohort-key, partial-advance, or blocked-decision change.
- No journal write, cleanup, schema migration, PostgreSQL behavior, or new persistent state.
- No resurrection of branch-only replay admission semantics.
- No change to the other `_job_is_terminal_success` consumers.

## Decisions

### D1. Narrow per-model success predicate at the breaker accessor

The occurrence loop keeps all existing provenance, identity, candidate, row-kind, and distinctness checks. Its success gate becomes:

1. aggregate terminal success; or
2. `status == "partially_failed"` and the bounded master `candidate_projections` contains this exact `model_id` with `array_task_outcome == "succeeded"`.

Aggregate-success masters continue to count without projections. A missing, malformed, truncated, duplicate, or failed model projection does not count. This preserves fail-toward-liveness and the existing 256-member bound. The other `_job_is_terminal_success` call sites stay unchanged.

Alternative rejected: expand/collapse masters into per-model rows before counting. That entangles the existing master-plus-terminal distinctness rule and increases the overcount risk.

### D2. Evaluate the cohort fully, then preserve the same conjunction

The forced-resubmit gate computes each active basin's existing qualification from the same decision whitelist and canonical restart-stage comparison. The result remains `all(qualifications)`. Full evaluation allows it to count qualifying basins and identify the first non-qualifying basin in stable input order; it does not admit any new decision.

A veto record is created only when at least one basin qualifies and at least one does not. All-qualifying cohorts return `True` with no record; cohorts with zero forced-resubmit requests return `False` with no misleading veto incident.

### D3. Keep one invocation-local fixed-shape veto record

`CycleOrchestrationContext` owns at most one record for the whole orchestration invocation. Later stage checks never overwrite it. The record contains only bounded scalars:

- schema/reason tokens;
- cycle id, pipeline run id, terminal job id and canonical job stage;
- cohort size and qualifying request count;
- first veto candidate/model/basin identity;
- veto decision, canonical restart stage, and stable veto cause.

No lists, raw state evidence, paths, logs, secrets, or journal content enter the record.

### D4. Reuse candidate outcomes and the scheduler receipt

`candidate_outcomes` attaches the record only to the matching veto candidate. Scheduler execution evidence projects it as a named top-level field on that candidate row, and bounded candidate summarization retains the same fixed-shape field. This avoids a new `PipelineResult` API field and avoids fanning a cohort fact onto every basin.

Alternative rejected: a process-global/orchestrator attribute or journal event. The scheduler runs cohort workers concurrently, and a shared attribute repeats the prior cross-worker race class; a journal event would violate the observe-only boundary and make a decision probe mutate authority.

### D5. Documentation and oracle routing

The §8.7 runbook states which aggregate/per-model master outcomes arm the breaker and its truncation fallback. The dependency-chain spec defines the veto receipt and explicitly freezes eligibility. Focused tests plus ruff/OpenSpec run locally and on node-27; node-22 Slurm runtime validation is not required because no sbatch, gateway, resource, or submission-decision behavior changes.

### D6. Commit-time structural ratchet owns the new logic

The repository's `large-file-guard` rejected the initial implementation because adding the new evaluator/outcome code to already-oversized owner modules and adding tests to the historical warm-start suite would perpetuate files above 1000 lines. The final ownership is therefore:

- `chain_forced_resubmit.py` owns the forced-resubmit predicate and bounded veto record; `chain_forecast_orchestrator_cycle.py` keeps the pre-existing import/call surface as thin forwarding aliases/wrappers.
- `chain_array_evidence.py` owns candidate-outcome construction and evidence sanitization; `chain_array_accounting.py` keeps its compatibility functions and dependency injection surface as thin wrappers.
- `test_forced_resubmit_veto.py` owns this change's veto matrix; `test_warm_start_chaining.py` returns to its pre-change content and is not committed by this change.

This is a tooling-required implementation-path deviation, not a semantic expansion. No existing import or monkeypatch path may disappear, every newly authored source module must remain below 1000 lines, and the commit hook plus structural entropy tests are required evidence.

## Invariant Matrix

- **Governing invariant:** retry eligibility is unchanged; each proven quarantine submission counts at most once for a model, and each mixed-cohort veto returns at most one truthful bounded record.
- **Source-of-truth identity/contract:** master provenance + `init_state_identities` + model-bound `candidate_projections`; current basin `state_evidence.decision` + canonical restart stage.
- **Producers:** forecast reservation/reconcile master evidence; scheduler candidate state evidence; forced-resubmit evaluator creates the invocation-local veto record.
- **Validators/preflight:** accepted-submit normalization/bounds; per-model breaker success predicate; existing decision whitelist and stage-order comparison.
- **Storage/cache/query:** file journal remains read-only; `CycleOrchestrationContext` is invocation-local; no DB/cache schema change.
- **Public routes/entrypoints:** `ForecastOrchestrator.orchestrate_cycle` and production scheduler cohort execution.
- **Frontend/downstream consumers:** `PipelineResult.candidate_outcomes`, scheduler candidate execution evidence, bounded scheduler receipt; no frontend change.
- **Failure paths/rollback/stale state:** malformed/truncated projection undercounts to zero; zero-request cohorts emit no incident; no write means rollback is code rollback only.
- **Evidence/audit/readiness:** focused pytest, bounded-compaction regression, ruff, strict OpenSpec, runbook text.
- **Regression rows:**
  - provenance master `partially_failed`, target projection succeeded -> count 1 and breaker may arm;
  - target projection failed/missing/malformed or master failed -> count 0;
  - aggregate-success master without projections plus reconciled terminal copy -> count exactly 1;
  - mixed eligible/ineligible cohort -> boolean false plus one record on first veto candidate;
  - all eligible / zero eligible / branch-only marker-shaped input -> current boolean semantics with no invented admission.

## Boundary-Surface Checklist

- Shared helper roots: breaker accessor success predicate and chain candidate-outcome builder.
- Public entrypoints/read surfaces: file-journal accessor, orchestrate-cycle result, scheduler receipt.
- Write/delete/overwrite surfaces: none; verify journal bytes unchanged.
- Producer/consumer evidence boundaries: context -> candidate outcome -> scheduler evidence -> bounded summary.
- Stale-state/idempotency boundaries: first record wins per invocation; repeated reads do not mutate or double-count.
- Unchanged downstream consumers: legacy aggregate-success rows, per-model terminal identity readers, partial-advance, both forced-resubmit whitelist copies.

## Risks / Trade-offs

- **Projection entry absent after the 256-member bound** -> deliberately undercount and leave the breaker disengaged; document and test the fail-toward-liveness rule.
- **Evidence-only refactor accidentally changes eligibility** -> compare the full qualification matrix and pin all-eligible, mixed, zero-eligible, restart-stage, and archived-marker-shaped inputs.
- **Receipt disappears under evidence pressure** -> retain the fixed-shape field in bounded candidate summarization and test that path.
- **Candidate identity cannot be matched** -> production-normalized basins have candidate ids; if absent, preserve the boolean result and omit rather than misattach evidence.

## Migration Plan

Additive code and evidence only; old journals and consumers remain valid. Deploy through the normal scheduler release. Rollback is a code revert; no data migration or cleanup is needed.

## Open Questions

None.