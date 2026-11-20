# Proposal

## Why

The orchestration core is where state-machine fixes keep coming back. In the last 120 days there were 154 `fix(scheduler|retry|journal|chain|orchestrator|ops|succession)` commits, 41 of them about retry/attempt accounting. Two things carry most of that cost. First, a second orchestration write path (SQL) that no production entry point reaches but that every change still has to keep working. Second, retry/attempt rules that are written out in about seven places. Around both sit three import-time compat facades and their inventories, which exist only to keep old test monkeypatch paths alive, and a single 11k-line journal repository class. Epic #3217 (owner: "不必顾及工作量，只要是为了本项目的整体优化，都可以做") asks for the redesign. The owner settled the four design branches in the Stage 1 gate (see design.md, Decisions).

## What Changes

- **BREAKING (non-production surfaces only)**: retire the SQL orchestration write path:
  - `PsycopgOrchestratorRepository` (`services/orchestrator/chain_repository.py`);
  - the `nhms-pipeline trigger-analysis` subcommand and `AnalysisOrchestrator`;
  - the non-DB-free scheduler mode (`_default_adapters`, the SQL reconcile fallback, the Postgres scheduler lease);
  - the SQL `RetryService`, `get_retry_service` and `_retry_service_from_env`, and the `PipelineStore` write methods that only they used.

  The DB-free file journal becomes the scheduler's only mode. node-22 production already runs only in that mode, and node-27 runs no scheduler. `NHMS_SCHEDULER_DB_FREE_REQUIRED` no longer selects the scheduler mode but stays as the gateway/worker job-env contract. The scheduler writes `scheduler_db_free_required=true` into every manifest.
- Retry/attempt accounting gets one owner, `services/orchestrator/retry_identity.py`:
  - it owns retry job id formatting, next-attempt minting, stage attempt reads, the retry budget limit and the retryable status sets;
  - every call site delegates to it;
  - a table-driven unit test pins the historical cases (#1845, #2254, #2393, #2394, #2404, #2416, #2542) and today's divergent paths exactly as they behave now.

  Unifying the paths that ignore the `_retry_<n>` suffix is a separate, later behaviour change. It is not part of this change.
- Clean up the compat facades narrowly. Late binding stays: owner modules keep reading names through `services.orchestrator.chain` / `services.orchestrator.scheduler` at call time, so test monkeypatches keep working.
  - The production helpers that are defined in compat modules today (`_now`, `_sleep`, `_bounded_evidence_payload`, `_evidence_status`, `_utcnow`) move to owner modules and are imported explicitly.
  - These are deleted: the programmatic installers (`globals().update` of `chain_compat_static.py`, `chain_compat_runtime.py`, `scheduler_compat_runtime.py`, `scheduler_candidate_runtime.EXPORTS`, `install_scheduler_state_compat`), every `_*_COMPAT_*` self-check table, `docs/governance/{CHAIN,SCHEDULER}_COMPATIBILITY_INVENTORY.md`, and the entropy-audit facade guard.
- Split `FileOrchestrationJournalRepository` into responsibility mixins in sibling modules. These are pure moves: same class name, import path, public and private method names, and on-disk format. Order: retention → status/events + identity-blocked release → reservation reclaim, operator demote/bind, cohort projection → submit lifecycle → reconcile inventory. A characterization guard lands first. The shared core (C1–C6, C8–C11) stays in place.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `node22-dbfree-runtime`: the DB-free runtime is no longer an opt-in mode next to a DB-backed one. It is the only scheduler runtime. The fail-closed rejection of DB-backed settings stays.
- `analysis-run-pipeline`: the orchestrated analysis lane (`trigger-analysis` and analysis run creation by `AnalysisOrchestrator`) is removed. It existed only on the SQL path.
- `file-orchestration-journal`: the contract-test requirement no longer leans on `PsycopgOrchestratorRepository` tests for DB-backed semantics.
- `job-retry-mechanism`: adds the single-owner requirement for retry/attempt accounting and its table-driven characterization.
- `scheduler-facade-deepening`, `chain-facade-deepening`: the facades keep their entrypoints and the late-binding monkeypatch surface. They stop installing names programmatically and stop keeping compatibility inventories.
- Further capabilities whose text names deleted symbols are rewritten by the slice that deletes them (design.md Appendix D): `pipeline-job-persistence`, `production-scheduler-orchestration`, `real-slurm-gateway-contract`, `slurm-array-runner-integration`, `facade-shrink`, `scheduler-discovery-evidence-owner-compat`, `import-layering-guard`. `compatibility-facade-governance` is removed.

## Impact

- Code: `services/orchestrator/` (chain*, scheduler*, retry*, file_orchestration_journal*, cli.py, persistence.py), `apps/api/routes/pipeline.py`, `apps/api/readonly_validation_probe.py`, `scripts/governance/entropy_audit/`.
- Tests: large retargeting in `tests/test_production_scheduler.py` and `tests/test_orchestration_chain.py`. Deletions limited to tests whose only subject is the retired path.
- Docs/specs: the governance inventories, `services/orchestrator/AGENTS.md`, `infra/env` templates and README, the two-node runbooks.
- Production:
  - node-22 behaviour is unchanged: it already runs DB-free.
  - Each deploy is verified with idle and submitting scheduler passes, at the cadence set in design.md.
  - node-27 display read routes, the provenance importer and the operator rollback tool are untouched.
- No change to journal on-disk formats, the SQL schema or the published API contract. The cancel route's fate is #3245.
