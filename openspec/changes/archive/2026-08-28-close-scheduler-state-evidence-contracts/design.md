## Context

The active project profile is NHMS. This is a `broad-expanded` fixture with high repair intensity: it spans the shared scheduler state/attempt owner, a DB repository boundary, Slurm submission provenance, bounded evidence discovery, and a closed JSON receipt schema. The five issues are grouped because each is a producer/consumer truth gap; the PR must reduce, not multiply, sources of truth.

Current behavior has four splits:

- explicit stage reads compute `max(flat retry_count, matching-stage rows/floors)`, so an unrelated stage can charge a budget and a truncated flat carrier can change the answer;
- the file-journal supplies all rows before projection, while PostgreSQL cuts to `job_limit + 1` rows before the shared floor computation and therefore cannot report the true total;
- candidate submission evidence treats a concrete Slurm ID as both “submitted” and “submit was called”, although a producer-proven gateway-crossed ambiguous result knows the latter only as `unknown_after_attempt`;
- pass-file and registry skip-cause consumers have narrower vocabularies than their producers.

## Goals / Non-Goals

**Goals:**

- Give stage-scoped retry budgets one stage-matching truth on DB and file-journal paths.
- Preserve the distinction between confirmed submission, unknown after a real attempt, and proven no attempt through full and bounded evidence.
- Make scheduler pass discovery and retention agree on what a pass artifact is.
- Carry every publishability cause that can make a registry model partial through machine-readable receipts.
- Preserve old receipts, stage-less retry evidence, pure-freshness row selection, and unchanged sibling consumers.

**Non-Goals:**

- Do not redefine the top-level flat `retry_count`; it remains a window-local, stage-less compatibility/evidence value.
- Do not change retry limits, manual-retry authorization, candidate identity filtering, or file-journal writes.
- Do not change `submitted` to true without a confirmed Slurm identity, infer attempts from a status token alone, or change gateway/reconcile/retry policy.
- Do not rename or rotate the no-progress tracker, weaken pass-evidence validation, or change explicit single-file readiness input.
- Do not change registry publishability, checksum discovery, `missing_required_files` set-equality consumers, or require the additive receipt key in historical receipts.
- Do not add a DB schema migration, touch frontend/display behavior, or require node-22 live Slurm execution.

## Decisions

1. **Explicit stage reads exclude the flat aggregate.** Canonical stages read only the existing matching-stage row scan plus identity-narrowed `stage_retry_attempt_floors`; non-canonical explicit stages read only their raw-stage candidate rows. Only a stage-less read keeps the existing flat-first fallback. This closes both #1579 failure modes without inventing a “master row” owner that production cannot reliably identify and whose clean reservation count is normally zero.

2. **The DB path supplies complete scoped job input to the existing projection.** Remove the upstream freshness limit for the already candidate/cycle-scoped job query and let `candidate_state_from_rows` remain the sole owner of pure-freshness top-`job_limit` selection, stage floors, true `pipeline_jobs_total`, and `state_truncated`. This is simpler and safer than duplicating `DOWNSTREAM_STAGE_ALIASES` and `effective_retry_attempt` as SQL `CASE`/regex logic. Event truncation remains unchanged. The scoped query is expected to be small; focused integration evidence must include an over-window geometry and prove the externally returned row list stays hard-bounded.

3. **Submission confirmation and submit-call provenance are separate values.** A confirmed Slurm identity alone makes `submitted=true`. A producer-owned ambiguous result emitted after the gateway boundary and durable accepted-submit transition makes `slurm_submit_called=unknown_after_attempt` while `submitted=false`; the exact producer provenance must be stronger than the status token alone. The tri-state value feeds execution mutation/proof and compaction through existing `UNKNOWN_AFTER_ATTEMPT` support. A hand-constructed or replayed token without that provenance stays false/proven-absent.

4. **Pass filename classification has one shared owner.** Move the governed `scheduler_` prefix and accepted JSON suffix predicate to a scheduler evidence module usable by both retention and readiness. Root discovery applies it before sorting/capping. Explicit `--scheduler-evidence-file` remains an operator-selected file and is not reclassified by the discovery predicate.

5. **The skip-cause key set expands atomically.** Add `unreadable_required_files` to publisher skip rows/diagnostics, the refresh key set and bounded projection, runtime receipt validation, and the JSON Schema in the same commit. The key uses existing collection/string bounds and remains optional, so historical receipts validate unchanged. Existing consumers that intentionally compare only `missing_required_files` are untouched.

## Risk Fixture

Fixture level: expanded
Project profile: NHMS
Repair intensity: broad-expanded
Upstream suggested level: expanded/high-risk for #1692; absent for the other legacy issues (override upward to broad-expanded because one PR spans shared state helpers, DB state, evidence provenance, discovery, and a closed schema).

Change surface:
- `services/orchestrator/chain_repository_state.py`
- `services/orchestrator/scheduler_state_rows.py`
- `services/orchestrator/scheduler_candidate_execution_evidence.py`
- scheduler evidence filename owner and readiness discovery/retention consumers
- `scripts/publish_scheduler_file_registry.py`
- `scripts/scheduler_file_provider_refresh.py`
- `schemas/scheduler_file_provider_refresh_receipt.schema.json`
- focused tests, OpenSpec deltas, and operator/governance docs

Must preserve:
- File-journal `pipeline_jobs` selection remains pure-freshness top-`job_limit`, and carried floors never add or evict rows.
- Stage-less retry reads remain flat-first and do not consume stage floors.
- Candidate identity filtering narrows rows and floor contributors through the existing authority predicates.
- No confirmed Slurm ID means `submitted=false`; real proven-no-submit and confirmed-submit controls remain unchanged.
- Readiness validation strength, evidence byte limits, registry refusal/publish gates, old receipt validation, and missing-file-only consumers remain unchanged.

Seams under test:
- `_state_retry_attempt` via projected candidate state, including canonical, raw-stage, and stage-less reads.
- An `@pytest.mark.integration` `PsycopgOrchestratorRepository.candidate_state` regression against node-27 `integration_database_url`, using real `ops.pipeline_job` inserts and compared with file-journal projection on the same row geometry; `CapturingRepository`/SQL stubs are non-oracles.
- Real `ProductionScheduler` candidate evidence through model-run, execution proof, no-mutation proof, persisted artifact, and bounded compaction.
- Readiness root discovery/item assembly with mixed tracker/pass files and the discovery cap.
- Registry publisher skip sink through `registry_cutover_removal_refused`, runtime validation, and JSON Schema validation of the same receipt.

Risk packs considered (core):
- Public API / CLI / script entry: selected - readiness and registry publisher entry behavior is operator-visible.
- Config / project setup: not selected - no config keys/defaults change.
- File IO / path safety / overwrite: selected - shared evidence-root discovery must classify files without changing deletion scope.
- Schema / columns / units / field names: selected - an optional receipt field and tri-state evidence semantics change; no DB schema changes.
- Auth / permissions / secrets: not selected - no authority or secret surface changes; existing redaction remains mandatory.
- Concurrency / shared state / ordering: selected - DB snapshot/state projection and durable accepted-submit provenance must not be reordered or fabricated.
- Resource limits / large input / discovery: selected - DB input exceeds `job_limit`, readiness discovery is capped, and receipt lists remain bounded.
- Legacy compatibility / examples: selected - historical receipts, stage-less readers, file-journal behavior, and explicit-file readiness remain compatible.
- Error handling / rollback / partial outputs: selected - ambiguous attempts and unreadable files must remain explicit through bounded evidence.
- Release / packaging / dependency compatibility: not selected - no dependency or packaging change.
- Documentation / migration notes: selected - governance inventory, runbook, and archived requirements must match runtime behavior.

Domain packs:
- Geospatial / CRS / basin geometry: not selected - no spatial behavior.
- Hydro-met time series / forcing windows: not selected - no forcing data/time-window behavior.
- SHUD numerical runtime / conservation / NaN: not selected - no solver behavior.
- PostGIS / TimescaleDB domain behavior: selected - real PostgreSQL candidate-state projection is the required oracle, without a schema migration.
- Slurm production lifecycle / mock-vs-real parity: selected - submit-call provenance crosses the gateway/evidence boundary, while scheduling behavior itself is unchanged.
- External hydro-met providers / snapshot reproducibility: not selected - no provider interaction.
- Run manifest / QC provenance: selected - full, persisted, and compacted scheduler evidence must carry the same tri-state fact.
- Published NHMS artifacts / display identity: not selected - no display/publication identity changes.

## Invariant Matrix

- Governing invariant: Every scheduler decision or operator receipt SHALL derive from the authoritative scoped state/provenance for that exact stage/artifact and SHALL never promote a truncated aggregate, unrelated row, status token, or non-pass file into stronger evidence.
- Source-of-truth identity/contract: candidate/run/model/cycle/stage identity; `effective_retry_attempt`; identity-narrowed stage-floor contributors; confirmed Slurm job ID versus producer-proven gateway attempt; governed pass filename; inventory cause-key set and receipt schema.
- Producers: PostgreSQL/file-journal pipeline rows; chain stage execution; scheduler pass writer; basins discovery and registry publisher.
- Validators/preflight: candidate-state projection and identity filter; execution proof builder; readiness scheduler evidence validator; refresh runtime validator and JSON Schema.
- Storage/cache/query: `ops.pipeline_job`; file orchestration journal; scheduler evidence root; persisted/full/bounded pass artifacts; refresh receipt.
- Public routes/entrypoints: production scheduler/readiness CLI and registry publisher/refresh scripts; no API route changes.
- Frontend/downstream consumers: operators, merge/readiness evidence checks, and historical receipt readers; frontend is unchanged.
- Failure paths/rollback/stale state: reverse truncation geometry, cross-stage flat carrier, gateway-crossed empty-ID ambiguity, token-only pending, mixed tracker/pass root, unreadable required file, old receipt without the additive key.
- Evidence/audit/readiness: model-run evidence, execution/no-mutation proofs, bounded payload, readiness items, registry cutover refusal receipt, node-27 real-DB test output.
- Regression rows:
  - forecast attempt row older than `job_limit` unrelated rows -> DB and file-journal stage-scoped attempt both return N; returned job list stays at `job_limit`; DB total is exact.
  - convert flat count 5 with no forecast retry -> explicit forecast/download attempts ignore 5 while stage-less evidence still reads the existing flat value.
  - gateway-crossed ambiguous submit with empty ID -> `submitted=false`, submit call/mutation unknown, no proven absence, and compaction preserves it.
  - token-only reconciliation status without producer provenance -> `submitted=false`, `slurm_submit_called=false`, execution proof `slurm_submit_proven_absent=true`, and no-mutation proof remains proven-absent; no positive or unknown evidence is manufactured.
  - shared root containing tracker, temp, unrelated JSON, passed pass, and stable-blocked pass -> only governed `scheduler_*.json` pass artifacts enter readiness or the cap.
  - unreadable required registry file -> refusal receipt names the file in a bounded optional list and validates identically at runtime and under JSON Schema.
  - historical receipt without `unreadable_required_files` -> still validates and reconstructs unchanged.

Boundary-surface checklist:
- Shared helper roots: retry stage/attempt owner; scheduler pass filename predicate; registry skip-cause key set.
- Public entrypoints/read surfaces: DB candidate state, readiness root scan, refresh receipt validation.
- Write/publish surfaces: scheduler evidence projection/compaction and registry receipt emission; no new write authority.
- Producer/consumer evidence boundaries: chain result -> model-run/proof; discovery inventory -> publisher -> refresh/schema; pass writer -> readiness/retention.
- Stale/idempotency boundaries: SQL pre-truncation, identity-filtered floors, token-only pending, old receipts.
- Unchanged downstream consumers: stage-less evidence/manual defaults, file-journal projection, explicit readiness file, missing-file set equality, frontend/display.

## Risks / Trade-offs

- [DB candidate history can exceed the former fetch window] -> keep the SQL predicate candidate/cycle-scoped, preserve the returned `job_limit`, and exercise over-window input on the real DB oracle; do not replace correctness with a silent upstream cut.
- [A truthy tri-state string could accidentally set `submitted=true`] -> compute confirmed submission as a separate boolean and test all three submit states end to end.
- [A shared filename predicate could reject historical unprefixed pass files] -> current pass producers already use `scheduler_`; explicit-file mode remains available, and root-scan behavior intentionally narrows.
- [Adding a receipt key can break strict readers] -> update writer, runtime allow-list, schema, and tests atomically; keep the field optional.
- [The broad PR has independent surfaces] -> implementation uses one owner per shared root, focused tests per issue, six-lens review, and one end-to-end invariant matrix.

## Migration Plan

1. Land code, schema, tests, specs, and docs in one PR; no data migration is required.
2. Run local focused tests/lint/OpenSpec validation, then node-27 real-DB candidate-state verification on the exact pushed head.
3. Existing receipts and journal rows require no rewrite. Rollback is a code rollback; additive optional receipt data can be ignored by the prior reader, but writer/reader/schema changes must roll back together.

## Open Questions

None. Issue acceptance criteria and current authoritative helpers determine the behavior.
