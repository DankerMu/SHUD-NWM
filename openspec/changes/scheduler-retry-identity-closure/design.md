## Context

The file-journal candidate projection is intentionally cycle-wide and bounded. #1179 carries canonical-stage attempt maxima across row truncation in `stage_retry_attempt_floors` plus exact contributor records in `stage_retry_attempt_floor_sources`; candidate filtering narrows those carried values, but the strict-warm-start raw-state read still scans unfiltered in-window rows (#1586). The shared-cycle aggregate filter intends to preserve a source-cycle download failure, but `_top_level_source_cycle_download_blocker` first compares the candidate state's own top-level `run_id` to the source-cycle identity and therefore rejects every real projection before inspecting the blocker row (#1584). Geometry B deliberately leaves the failed row outside the row window while retaining its floor; manual minting still asks the row-derived failed-stage resolver and falls to attempt zero (#1577).

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

## Goals / Non-Goals

**Goals:**

- Apply one candidate-authority predicate to both components of the strict-warm-start stage attempt: carried floors and in-window row scans.
- Recognize and preserve a top-level source-cycle download blocker only when a concrete blocker row proves the expected source/cycle identity.
- Recover a canonical stage for geometry-B manual minting from exact adopted-marker/floor-source lineage and mint from the durable attempt floor.
- Preserve geometry-B projection visibility: top-level `failed_stage`/`stage`/`restart_stage` remain empty and `_failed_stage`/`_candidate_failed_stage` remain unresolved when the failed row is outside the window.
- Preserve all existing public evidence shapes, pure-freshness row selection, retry-limit/manual override semantics, E13e floor narrowing, and unaffected state consumers.

**Non-Goals:**

- Fix the flat top-level `retry_count` cross-stage/window behavior (#1579), DB-side SQL truncation (#1572), or non-canonical-stage floor truncation.
- Change marker persistence schemas, API contracts, force-resubmit lists, Slurm behavior, or row selection.
- Make ambiguous marker lineage guess a stage, parse stage from job-id text, or run the full candidate decision filter at the strict-warm-start raw-state read.

## Decisions

### D1. Scope the strict-warm-start attempt at its candidate-aware read point

Create a read-only attempt-state view at the strict-warm-start budget boundary. It reuses `_candidate_identity_from_evidence`, `_state_row_has_authoritative_candidate_proof`, and the existing source-cycle blocker escape to filter the row-scan population, while reusing `_candidate_authoritative_stage_retry_attempt_floor_state` for carried floors. It must not mutate the caller's raw state.

This stays at the read point because `_state_retry_attempt` has no candidate identity and has valid scope-blind consumers. Skipping every `_job_is_cycle_scope_row` globally is wrong: the bare `cycle_<source>_<stamp>` wedge is authoritative for every candidate and must continue to bind #1179 E5, while a suffixed `..._forecast_cohort_<digest>` row is authoritative for none. Running `_candidate_state_decision_state` is also forbidden because its shared-cycle aggregate strip removes the authoritative wedge floor.

### D2. Bind the top-level blocker to blocker-row identity

`_top_level_source_cycle_download_blocker` SHALL validate the top-level failure/stage shape, then obtain identity proof from a concrete source-cycle download blocker row using the same `_global_source_cycle_download_blocker_job` / `_source_cycle_identity_matches_expected` chain as row filtering. The candidate state's top-level `run_id` is not blocker identity and is ignored for this decision. A top-level-only failure with no matching row proof fails closed rather than being accepted by inference.

This makes the existing restore branch and shared-cycle blocker branch reachable without adding another identity vocabulary. Replacing the top-level candidate `run_id` with a synthetic cycle run id, as the old E13e guard test did, is no longer acceptable evidence; the new test must use an unmodified real projection. The old E13e invariant itself remains load-bearing: on that now-real blocker branch, `_narrow_stage_retry_attempt_floors` must still remove every non-candidate-authoritative contributor while preserving blocker evidence. Wrong-source/wrong-cycle blocker rows remain rejected.

### D3. Recover geometry-B stage only from exact marker lineage

When `_candidate_failed_stage(state)` returns a stage, the current path remains authoritative. Otherwise, only the manual-retry evidence/mint path inspects the newest adopted marker under the same terminal scan semantics already used by `_manual_retry_payload`. Collect its exact target identities (`entity_id` and recorded previous/failed job identifiers) and compare them to `stage_retry_attempt_floor_sources`. A canonical stage is recoverable only when an authoritative retained contributor has an exact identifier match; `details.failed_stage`, when present, must agree by `_canonical_downstream_stage` identity and may disambiguate. Floor-source mapping keys are accepted only in their canonical spelling, matching the sole producer; an alias-spelled or non-canonical hand-shaped key fails closed rather than being returned as a supposedly canonical stage. A unique matching canonical stage is passed to `_state_retry_attempt`; no match or multiple disagreeing stages keeps the existing stage-less fallback. Neither `_failed_stage` nor `_candidate_failed_stage` is widened, and the projection does not restore the truncated failed row or populate any top-level stage key.

This reuses two already persisted/projection-carried sources of truth: #1308's marker write-time `failed_stage`/target lineage and #1179's floor contributors. It adds no state key. It also supports legacy adopted markers that lack `failed_stage` when their exact target row survives in floor-source lineage. Choosing the numerically largest floor's stage is rejected because unrelated stages can tie or exceed the repair target; parsing a job-id token is rejected because production ids stack stage/retry tokens.

### D4. Compatibility and oracle integrity

Existing #1179 tests remain immutable anchors: the bare cycle wedge still blocks at the limit, out-of-window foreign floors stay narrowed, nameable manual minting remains `N+1`, stage-less consumers ignore floors, and pure-freshness selection is unchanged. New tests must first demonstrate red behavior against pre-change runtime source, then turn green without weakening those anchors.

## Selected Risk Packs

- Schema / columns / units / field names: selected — existing floor-source and marker-detail fields become load-bearing; no new field is introduced.
- Concurrency / shared state / ordering: selected — retry, blocker restore, and manual marker ordering are shared state-machine decisions.
- Legacy compatibility / examples: selected — markers without #1308 `failed_stage` must retain exact-lineage fallback; unrelated old state shapes remain unchanged.
- Error handling / rollback / partial outputs: selected — wrong identity/ambiguous lineage must fail closed to retry/no inference, never silently block or mint a colliding key.
- Documentation / migration notes: selected — code/runbook boundary language must stop describing the repaired gaps as accepted behavior.
- Slurm production lifecycle / mock-vs-real parity: selected — decisions govern whether forecast work is blocked or reminted, but no sbatch/Slurm runtime behavior changes; scheduler pytest is the oracle.

All other core packs are not selected: Public API / CLI / script entry (private helpers and unchanged output schema); Config / project setup (no setting change); File IO / path safety / overwrite (no file operation change); Auth / permissions / secrets (candidate data identity, not access control); Resource limits / large input / discovery (bounded row sets unchanged); Release / packaging / dependency compatibility (no dependency/package change). Other NHMS domain packs are not selected: no geospatial, hydro-met window, numerical, PostGIS/Timescale, external-provider snapshot, manifest/QC provenance, or published-artifact identity behavior changes.

## Invariant Matrix

- Governing invariant: every retry attempt, blocker restoration, and manual mint decision SHALL be derived from evidence authoritative for the same candidate or source cycle and exact repair lineage; truncation may hide rows but may not authorize foreign evidence or force an ambiguous guess.
- Source-of-truth identity/contract: `candidate_identity`; source/cycle blocker row identity; adopted marker `entity_id`/`previous_job_id`/`failed_stage`; `stage_retry_attempt_floors` and `stage_retry_attempt_floor_sources`.
- Producers: `candidate_state_from_rows`, `stage_retry_attempt_floors`, `FileOrchestrationRetryService.record_manual_repair` (unchanged bytes).
- Validators/preflight: `_state_row_has_authoritative_candidate_proof`, `_source_cycle_identity_matches_expected`, `_event_is_adopted_manual_retry_marker`, exact marker/floor-source lineage resolver.
- Storage/cache/query: file-journal projected state and marker events; DB query semantics unchanged and #1572 remains out of scope.
- Public routes/entrypoints: scheduler candidate decision; manual retry request remains unchanged at the API boundary.
- Frontend/downstream consumers: scheduler submission selection and evidence consumers; payload keys remain compatible.
- Failure paths/rollback/stale state: foreign cohort row ignored; unmatched blocker not restored; ambiguous/stale marker keeps existing stage-less fallback; manual retry remains allowed beyond automatic limit.
- Evidence/audit/readiness: focused production-scheduler regressions, predecessor #1179 anchors, ruff, strict OpenSpec validation, and node-27 backend verification.
- Regression rows:
  - in-window non-authoritative canonical cohort attempt at/above limit -> strict-warm-start remains retry; bare cycle wedge and candidate-owned row still bind.
  - real projected source-cycle download failure row -> top-level blocker survives candidate-state filtering, while a non-authoritative carried floor is narrowed on that same branch; wrong source/cycle row does not restore the blocker.
  - geometry B + adopted marker exact target lineage + floor `N` -> top-level stage keys stay empty, both failed-stage resolvers stay unresolved, and only manual evidence derives `previous_attempt=N`, `new_attempt=N+1`; ambiguous/foreign/no-lineage marker keeps existing fallback.
  - existing nameable-stage, explicit marker-attempt, stage-less consumer, row-selection, E13a-d/E13f, and floor-narrowing behavior -> unchanged; E13e's assertion is retained on the new unmodified real-projection geometry.

## Boundary-Surface Checklist

- Shared helper roots: candidate identity predicates, stage attempt derivation, marker adoption/target lineage.
- Public entrypoints: scheduler candidate decision and manual retry evidence composition.
- Read surfaces: projected jobs, floor/source mappings, marker events, blocker row identity.
- Write/delete/overwrite surfaces: no runtime writes changed; marker producer inspected for compatibility only.
- Staging/publish/rollback surfaces: none.
- Producer/consumer evidence boundaries: projection floor sources -> identity view; marker event -> manual mint; blocker row -> restored top-level decision.
- Stale-state/idempotency boundaries: exact marker target, newest adopted marker, no ambiguous stage inference, no `_retry_1` collision in geometry B.
- Unchanged downstream consumers: failure policy, cancelled evidence, evidence owner, force-resubmit whitelists, non-canonical retry arm.

## Risks / Trade-offs

- [Risk] Filtering too broadly could disarm the authoritative bare-cycle retry floor. -> Keep filtering candidate-aware and pin both cohort and wedge halves.
- [Risk] Treating the candidate top-level state as blocker identity could reintroduce the dead branch or accept a foreign blocker. -> Require a concrete matching blocker row and a negative source/cycle control.
- [Risk] Marker stage recovery could charge a different stage's floor. -> Require exact target-id intersection and a unique agreeing canonical stage; otherwise preserve fallback.
- [Risk] Older compacted state may lack floor sources. -> Preserve current behavior rather than guess; no compatibility break.

## Migration Plan

No data migration is required. Deploy the code and tests together. Rollback is a normal code revert because no persistent schema or marker bytes change. Node-27 is the backend oracle; node-22 validation is not required because no Slurm/sbatch/runtime scheduling resource behavior changes.

## Open Questions

None. The issue bodies and existing #1179/#1308 contracts determine all three choices.
