# Spec Delta

## Purpose
Every status, verdict and raw-state set has exactly one importable definition whose content provably equals the JSON schemas and the database enums.

## ADDED Requirements

### Requirement: `packages/common/status_vocab.py` SHALL be the single source of status sets
The module SHALL define frozensets `PIPELINE_JOB_STATUSES`, `PIPELINE_JOB_TERMINAL`, `PIPELINE_JOB_SUCCESS`, `HYDRO_RUN_STATUSES`, `DISPLAY_READY_RUN_STATUSES`, `CYCLE_STATUSES`, `SLURM_RAW_STATES`, `QC_VERDICTS` and matching `Literal` aliases. Each set SHALL be the union of the values the runtime currently produces at the sites recorded in design.md Context; `PIPELINE_JOB_TERMINAL` SHALL include `reservation_lost`; `PIPELINE_JOB_SUCCESS` SHALL be `{succeeded, complete, published}`; `QC_VERDICTS` SHALL be lowercase `{passed, failed, blocked}` with a `normalize_qc_verdict(text)` function mapping `pass`/`PASS`/`passed` to `passed` and likewise for the others.

#### Scenario: Terminal set contains the runtime-real value
- **WHEN** `PIPELINE_JOB_TERMINAL` is imported
- **THEN** it contains `reservation_lost`, `submission_failed`, `permanently_failed`, `failed`, `cancelled`, `skipped` and every success value

#### Scenario: Verdict normalization
- **WHEN** `normalize_qc_verdict("PASS")` is called
- **THEN** it returns `passed`; an unknown spelling raises `ValueError`

### Requirement: Schemas and the API SHALL agree with the vocabulary
`schemas/pipeline_job.schema.json` `status.enum` SHALL equal `PIPELINE_JOB_STATUSES`; `schemas/run_status.schema.json` `status.enum` SHALL equal `HYDRO_RUN_STATUSES`; `HYDRO_RUN_STATUSES` SHALL be a superset of the `hydro.run_status` enum in `db/migrations/000003_enums.sql`; `CYCLE_STATUSES` SHALL equal the `met.cycle_status` enum; the status tuple in `apps/api/routes/pipeline.py` and the OpenAPI enum it serialises SHALL be derived from `PIPELINE_JOB_STATUSES`. `tests/test_status_vocab_parity.py` SHALL assert each equality by reading the schema and migration files.

#### Scenario: Schema enum drift fails parity
- **WHEN** a value is added to `PIPELINE_JOB_STATUSES` but not to `schemas/pipeline_job.schema.json`
- **THEN** the parity test fails naming the missing value

#### Scenario: OpenAPI exposes the vocabulary
- **WHEN** the API status enum is generated
- **THEN** it lists every value of `PIPELINE_JOB_STATUSES` and `tests/test_api_contract.py` frontend-type parity still passes

### Requirement: No duplicate status literal sets SHALL remain outside the vocabulary
Every site listed in design.md Context (`services/orchestrator/{chain_types,chain_runtime_utils,file_orchestration_journal,scheduler,scheduler_candidates,persistence,scheduler_state_types,chain,chain_source_cycle,chain_repository_state,retry}.py`, `apps/api/routes/{pipeline,hydro_display_constants}.py`, `packages/common/{forecast_store,met_store}.py`, `workers/output_parser/parser.py`, `services/production_closure/{slurm_validation,met_validation,readiness_item_contracts,readiness_scheduler_evidence}.py`, `services/slurm_gateway/real_backend.py`, `services/m24_live/receipt.py`, `scripts/{run_qhh_continuous,node27_frontier_stall_alert,m24_gateway_proof}.py`, `scripts/basin_retirement/database.py`) SHALL import from `status_vocab` instead of defining a literal set. `tests/test_status_vocab_parity.py` SHALL scan non-test Python for any `frozenset`/`set`/`tuple` literal containing both `"succeeded"` and `"failed"` outside `status_vocab.py` and fail unless the file is in a shrinking allowlist `tests/status_literal_allowlist.json`.

#### Scenario: Copied set is detected
- **WHEN** a non-test file defines `frozenset({"succeeded", "failed", "cancelled"})` and is not allowlisted
- **THEN** the parity test fails naming the file and line

#### Scenario: Behaviour preserved at migrated sites
- **WHEN** the existing test suites of the migrated modules run
- **THEN** they pass unchanged
