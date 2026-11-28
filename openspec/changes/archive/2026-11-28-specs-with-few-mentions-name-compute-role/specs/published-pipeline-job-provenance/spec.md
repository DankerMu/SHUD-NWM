## MODIFIED Requirements

### Requirement: Source-owned published job provenance

The DB-free scheduler of the compute role SHALL publish a bounded, versioned `pipeline_jobs.json` record for each selected run, derived from the real file journal and cross-checked against the run's immutable scientific manifest. The record SHALL bind source, cycle, run, model, job identity, job type, stage, Slurm identity and lifecycle timestamps to the run's manifest and SHALL contain no private filesystem paths, credentials, URIs, or unpinned user payloads. A journal read SHALL never create directories, take a journal write lock, mutate scheduler state, or invoke Slurm submission/cancel/retry APIs.

#### Scenario: Successful export after a real terminal stage

- **WHEN** the existing run-tree copyback lifecycle reaches a terminal stage and a run's selected manifest/job set is complete
- **THEN** the producer publishes exactly one validated provenance record per run to `runs/<run_id>/input/pipeline_jobs.json`
- **AND** each job row preserves source-provided lifecycle timestamps, status, task metadata and verified log binding
- **AND** missing timestamps, exit codes or logs remain null rather than being inferred
- **AND** jobs whose journal `run_id`/`model_id` equal the selected run are included
- **AND** cycle-scoped journal rows, if included, keep their original cycle `run_id` and null `model_id`
- **AND** the subsequent run-tree copyback still runs

#### Scenario: Export refuses unsafe or mismatched evidence

- **WHEN** a requested run's manifest, selected journal rows, or task logs fail identity/schema/path/containment checks
- **THEN** the exporter records the failure without publishing or advertising provenance for that run
- **AND** an equal-version conflicting record cannot overwrite a newer already-applied row
- **AND** the failure does not alter scheduler/hydro terminal truth, source journal state, existing valid artifacts, or the subsequent run-tree copyback

#### Scenario: Publisher failure does not abort copyback

- **WHEN** provenance publication raises after a terminal stage that still has a valid scientific run tree
- **THEN** the failure is recorded in a bounded per-run summary
- **AND** `_copyback_stage_run_trees` still copies the run tree
- **AND** hydro terminal truth and scheduler state are unchanged
- **AND** the run is not advertised as having complete job provenance
