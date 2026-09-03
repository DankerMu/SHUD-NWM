## ADDED Requirements

### Requirement: Cycle-scope derivation from a run id recognises only the shapes a pipeline_job row can carry

The file journal's derivation of `(source_id, cycle)` from a run id, used by the run-id and idempotency-key single-row lookups, SHALL recognise exactly the forecast shape `fcst_{source}_{cycle}_{model}` and the cohort shape `cycle_{source}_{cycle}[_suffix]`. It SHALL NOT carry a derivation for the analysis shape `analysis_{source}_{start}_{end}_{model}`, because `pipeline_job` row identity validation rejects an analysis run id on both of its branches (with and without a model id) on every write and read path, so no such row can exist for the lookup to find. An analysis run id SHALL derive to "not derivable" and take the fall-open whole-tree scan, never "not found". The canonical analysis regex and `parse_run_cycle` in `run_identity` SHALL be unchanged, because retention consumes them for analysis workspaces.

The batch per-model row reducer SHALL have no flag governing direct-record participation: it never includes direct records and never stores into the cycle-rows cache. The "any flag that governs whether direct records participate SHALL retain its meaning unchanged in the narrowed read" clause of the cycle-scoped lookup requirement binds the narrowed single-row replay, not the batch reducer; the reducer's only ever-executed meaning (no direct records) is what remains. The latest-view materialiser's own routing flag SHALL keep its meaning (`True` reads through the fingerprinted cycle-rows path, `False` reads through the batch reducer), and every existing materialiser caller SHALL keep passing `False`. The sequence-floor computation SHALL be exposed only as the unlocked variant that write lanes call while holding the write lock; no lock-taking wrapper SHALL exist for it.

#### Scenario: Analysis run id rejected by identity validation on both branches

- **WHEN** `_validate_pipeline_job_identity` is applied to a row whose `run_id` is `analysis_era5_2026010100_2026010200_model_qhh.v1`, once with `model_id="model_qhh.v1"` and once with `model_id=None`
- **THEN** both calls raise `file_journal_run_mismatch`

#### Scenario: Analysis run id falls open

- **WHEN** `_cycle_scope_from_file_run_id("analysis_era5_2026010100_2026010200_model_x")` or `_cycle_scope_from_idempotency_key("analysis_era5_2026010100_2026010200_model_x:forecast")` is evaluated
- **THEN** the result is `None`, and `parse_run_cycle("analysis_era5_2026010100_2026010200_model_x")` still returns 2026-01-01T00:00Z

#### Scenario: Forecast and cohort derivations unchanged

- **WHEN** the existing narrowing tests derive from `cycle_ifs_<stamp>` and `fcst_<source>_<stamp>_<model>` run ids and the matching idempotency keys
- **THEN** they resolve the same `(source_id, cycle)` pairs as before

#### Scenario: Batch reducer rows unchanged and sequence floor reachable only unlocked

- **WHEN** the reconcile accounting and cohort-identity tests call the batch reducer without a direct-record flag, and the containment tests compute the sequence floor through the unlocked variant
- **THEN** every existing assertion passes verbatim and `FileOrchestrationJournalRepository` has no `_next_sequence` attribute
