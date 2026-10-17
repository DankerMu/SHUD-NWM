# Spec Delta

## Purpose
Every status, verdict and raw-state set has exactly one importable definition, each named set has one purpose, and the public sets provably equal the JSON schemas and the database enums.

## ADDED Requirements

### Requirement: `packages/common/status_vocab.py` SHALL be the single source of status sets, one named set per purpose
The module SHALL define one named set per (domain, purpose) pair with content **exactly equal to the canonical site it replaces**, never a union across sites, The vocabulary objects are immutable (`frozenset`, or `tuple` in the canonical site's order where the site is a tuple, because tuples are passed as DB parameters in `workers/output_parser/parser.py` and concatenated in `packages/common/met_store.py`). A site whose canonical object is a plain `set` today (`chain_types`, `chain_runtime_utils`, `file_orchestration_journal`, `scheduler`, `scheduler_candidates`, `persistence`, the `set`-typed members of `scheduler_state_types`) binds `set(<VOCAB>)` in the module that owns the shared object today, and only sites that already import that object today keep importing it (never adding an import against the existing direction; sites that hold independent objects today, such as `scheduler` and `scheduler_candidates`, each bind their own `set(...)`), so every site keeps its current type and identity; the parity test asserts each site object's type equals its pre-migration type. The public, closed sets are listed here by members; the per-purpose subsets are listed in design.md D3 by canonical site and their members SHALL be copied verbatim from that site.

- `PIPELINE_JOB_STATUS_ORDER` (tuple, the public closed enum): `pending, queued, submitted, running, succeeded, partially_failed, failed, submission_failed, reservation_lost, permanently_failed, cancelled, skipped`; `PIPELINE_JOB_STATUSES = frozenset(PIPELINE_JOB_STATUS_ORDER)`.
- `PIPELINE_JOB_INTERNAL_STATUSES = frozenset({reserved, cancellation_pending, reconcile_unverified})`: orchestrator-internal transients that SHALL NOT appear in any schema, API tuple or generated type.
- `PIPELINE_JOB_TERMINAL = frozenset({succeeded, partially_failed, failed, cancelled, submission_failed, reservation_lost, permanently_failed})` (the `chain_types` set); `PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION = PIPELINE_JOB_TERMINAL - {reservation_lost}` (the `scheduler`/`scheduler_candidates` sets, kept separate because changing them is a candidate-construction behaviour change); `PIPELINE_JOB_TERMINAL_GUARD` (the `persistence` four-value guard) and `PIPELINE_JOB_TERMINAL_OVERRIDES` (its two-value override set) stay distinct.
- `PIPELINE_JOB_SUCCESS = frozenset({succeeded, complete, published})` (`scheduler_state_types` binds `set(...)` once as the shared object today's six copies converge on): a read-side tolerance set (no pipeline-job writer produces `complete`/`published`; they are tolerated by six comparison sites), never added to the public enum.
- `HYDRO_RUN_STATUSES` = the `schemas/run_status.schema.json` enum (14 values); the existing hydro per-purpose sets `DURABLE_HYDRO_SUCCESS_STATUSES`, `HYDRO_RUN_CODE_CLEARING_STATUSES`, `FAILED_PIPELINE_STATUSES` and `TERMINAL_PIPELINE_SUCCESS_STATUSES` move from `services/orchestrator/scheduler_state_types.py` into the vocabulary as frozensets, and `scheduler_state_types` SHALL bind them under the same names with the same type as today (`set(...)` for the `set`-typed ones, the frozenset itself otherwise) so the #1581 same-object and type parity tests pass unchanged.
- `DISPLAY_PRODUCT_READY_STATUSES` (`{succeeded, parsed, published}`), `PARSE_READY_RUN_STATUSES` (`{succeeded, parsed, failed}`), `FAILABLE_RUN_STATUSES`, `HYDRO_RUN_TERMINAL_UI` (the API route's terminal hydro set), `FORECAST_CYCLE_SUCCESS_STATUSES` (the journal's four-value set) are distinct named sets.
- `CYCLE_STATUSES` = the `met.cycle_status` enum folded over all migrations; `CYCLE_TERMINAL_UI` (the API route's ten-value set), `CYCLE_TERMINAL_UPSERT` (`{published, cancelled}`, met_store) and `CYCLE_UPSERT_PRESERVED` (met_store) are distinct named sets.
- `SLURM_RAW_STATES` = the union of raw Slurm state names used by `slurm_validation`, `real_backend` and `run_qhh_continuous`; `SLURM_TERMINAL_STATES`, `SLURM_TERMINAL_SUCCESS`, `SLURM_TERMINAL_FAILURE` keep their current members.
- `QC_VERDICTS = frozenset({passed, failed, blocked})` with `normalize_qc_verdict(text)` mapping `pass`/`PASS`/`passed` to `passed` and likewise for the others, raising `ValueError` on an unknown spelling; `READINESS_ITEM_STATUSES` (`{passed, failed, blocked, not_executed, release_blocked}`) is a distinct set. `normalize_qc_verdict` SHALL be used only on read/compare paths; receipt writers and validators (`services/m24_live/receipt.py` `STATUS_VALUES`, the C4/e2e evidence schemas) keep their current spelling and are allowlisted as `distinct-vocabulary` where the case-sensitive scan reaches them (`services/m24_live/receipt.py` `STATUS_VALUES` is uppercase and carries no entry).

`Literal` aliases are derived from the ordered tuples.

#### Scenario: Named sets equal their canonical sites
- **WHEN** `tests/test_status_vocab_parity.py` runs
- **THEN** every named set equals the literal pinned in the test from its canonical site at `f7be77f22`, and `PIPELINE_JOB_TERMINAL` contains `reservation_lost` while `PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION` does not

#### Scenario: Verdict normalization
- **WHEN** `normalize_qc_verdict("PASS")` is called
- **THEN** it returns `passed`; an unknown spelling raises `ValueError`

#### Scenario: Receipt validator unchanged
- **WHEN** an existing m24 receipt fixture with `status: "PASS"` is validated after migration
- **THEN** it still passes `_require_enum`

### Requirement: Schemas and the API SHALL agree with the public vocabulary
`schemas/pipeline_job.schema.json` `status.enum` SHALL equal `PIPELINE_JOB_STATUS_ORDER` (same order); `schemas/run_status.schema.json` `status.enum` SHALL equal `HYDRO_RUN_STATUSES`; `HYDRO_RUN_STATUSES` SHALL be a superset of the `hydro.run_status` enum folded over every `CREATE TYPE`/`ALTER TYPE … ADD VALUE` under `db/migrations/` minus `frequency_done` (convergence-only, pinned by `tests/test_hydro_status_set_parity.py`); `CYCLE_STATUSES` SHALL equal the folded `met.cycle_status` enum; the status tuple in `apps/api/routes/pipeline.py` and the OpenAPI enum it serialises SHALL be `PIPELINE_JOB_STATUS_ORDER`. `tests/test_status_vocab_parity.py` SHALL assert each statement by reading the schema files and by reusing the migration-folding helper of `tests/test_hydro_status_set_parity.py`, parameterised by enum name so it folds `met.cycle_status` as well as `hydro.run_status`. The only public delta this change makes is `reservation_lost` joining the pipeline-job enum; it is recorded in design.md Non-Goals as the accepted exception.

#### Scenario: Schema enum drift fails parity
- **WHEN** a value is added to `PIPELINE_JOB_STATUS_ORDER` but not to `schemas/pipeline_job.schema.json`
- **THEN** the parity test fails naming the missing value

#### Scenario: ALTER-added enum value counts
- **WHEN** the folded `met.cycle_status` enum is computed over `db/migrations/`
- **THEN** it contains `cancelled` and `canonical_incomplete`, which only `ALTER TYPE … ADD VALUE` migrations add

#### Scenario: OpenAPI exposes the vocabulary in stable order
- **WHEN** the API status enum is generated under two different `PYTHONHASHSEED` values
- **THEN** both list `PIPELINE_JOB_STATUS_ORDER` in order and `tests/test_openapi_drift.py` and `tests/test_api_contract.py` pass

### Requirement: No duplicate status literal sets SHALL remain outside the vocabulary
Every site listed in design.md D3 SHALL import its named set from `status_vocab` instead of defining a literal. `tests/test_status_vocab_parity.py` SHALL scan non-test Python under `apps/ packages/ services/ workers/ scripts/` (excluding `status_vocab.py` itself) for any `frozenset`/`set`/`tuple` literal that equals, or is a subset or superset of, any vocabulary set with at least two members in common, plus any literal containing all of `"succeeded"`, `"complete"`, `"published"`, and fail unless the entry (`file` plus the literal's sorted members, so line moves do not stale it) is in `tests/status_literal_allowlist.json` with a reason from `{pending-migration, distinct-vocabulary, not-a-status-set}`. The initial allowlist SHALL be generated by running the scan on the tree before migration and adjudicating every hit; the exit condition of the migration is **no entry with reason `pending-migration`**, not an empty file.

#### Scenario: Copied set is detected
- **WHEN** a non-test file defines `frozenset({"succeeded", "failed", "cancelled"})` and is not allowlisted
- **THEN** the parity test fails naming the file and line

#### Scenario: Behaviour preserved at migrated sites
- **WHEN** the existing test suites of the migrated modules run
- **THEN** they pass unchanged (`tests/test_forced_resubmit_veto.py`, `tests/test_hydro_status_set_parity.py`, `tests/test_file_journal_full_tree_budget_contract.py` included)
