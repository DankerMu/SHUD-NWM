# published-pipeline-job-provenance Specification

## Purpose
TBD - created by archiving change restore-display-job-provenance. Update Purpose after archive.
## Requirements
### Requirement: Source-owned published job provenance

The node-22 DB-free scheduler SHALL publish a bounded, versioned `pipeline_jobs.json` record for each selected run, derived from the real file journal and cross-checked against the run's immutable scientific manifest. The record SHALL bind source, cycle, run, model, job identity, job type, stage, Slurm identity and lifecycle timestamps to the run's manifest and SHALL contain no private filesystem paths, credentials, URIs, or unpinned user payloads. A journal read SHALL never create directories, take a journal write lock, mutate scheduler state, or invoke Slurm submission/cancel/retry APIs.

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

### Requirement: Verified task log publication

The producer SHALL publish display logs only for jobs whose task/run/model identity is proven by the matching task entry in the existing gateway's parent-array response. It SHALL preserve truncation and source metadata, use canonical `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out` (and `.err` when stderr is present), and select exactly one matching task entry. Parent-envelope `run_id`/`model_id` SHALL NOT be used as the match key. Direct child log reads that lack metadata or identity SHALL NOT be used as authoritative display logs. Existing already-published logs MAY be reused only after containment and identity validation.

#### Scenario: Reconciled array task obtains its real log

- **WHEN** a reconciled forecast task row has an authoritative `array_task_id` and no `log_uri`
- **THEN** the producer reads the parent array response, selects exactly the matching task with `identity_complete=true` whose task-entry run/model equals the journal row
- **AND** publishes that task's stdout/stderr bytes under the canonical job log path
- **AND** the provenance row advertises the resulting URI only after publication succeeds
- **AND** a parent envelope whose `run_id` names a sibling array member is ignored

#### Scenario: Ambiguous or missing task log fails closed

- **WHEN** the gateway response has no matching task, has multiple/ambiguous matches, marks the task incomplete, or the matching task entry carries a different run/model identity
- **THEN** the run's log publication remains unadvertised for that task
- **AND** no master-level, parent-envelope, or nearby task log is substituted

### Requirement: Idempotent node-27 projection

Node-27 ingest SHALL import validated job-provenance records into the existing `ops.pipeline_job` read model in one transaction per run, including runs already parsed or published. The import SHALL be replay-safe, immutable under `job_id` for identity fields, and monotonic for lifecycle fields using source timestamps. Projection SHALL never re-run scientific parsing, force hydro state transitions, create credentials, grant additional privileges, or infer unavailable history. Projection failures SHALL be logged per run without poisoning unrelated runs.

#### Scenario: Existing published run gains job rows

- **WHEN** node-27 ingests a valid provenance record for a run already parsed and published, including a run skipped by the scientific ingest `done` set
- **THEN** the existing hydro coverage/readiness state remains unchanged
- **AND** new `ops.pipeline_job` rows appear with source, cycle, run, model, job and verified log metadata
- **AND** repeated import is a no-op when evidence is byte-identical
- **AND** cycle-scoped rows retain null `model_id` and are not rewritten onto the forecast run

#### Scenario: Older or conflicting source evidence cannot roll back rows

- **WHEN** node-27 sees an older provenance snapshot for an already-imported job
- **THEN** the newer row wins and the stale import is ignored
- **AND** when source and destination carry different identity fields for the same `job_id`, the import fails closed without partial updates
- **AND** a log URI is added only when it binds exactly to the same job and a newer or equivalent authoritative version

### Requirement: Backfill without scientific re-ingest

A bounded operator command SHALL explicitly backfill existing runs using the same publisher and importer as the normal lifecycle. It SHALL select only the requested runs, verify their manifests and journal authority, and record a summary that distinguishes publication failures from projection failures. The command SHALL not trigger forcing, parsing, coverage refresh, or publication state changes outside the job read model.

#### Scenario: Backfill a selected completed run

- **WHEN** the operator runs the backfill for one valid completed QHH run
- **THEN** the publisher writes its provenance record and verified log artifacts
- **AND** node-27 imports the resulting rows without re-running the scientific pipeline
- **AND** a later replay reports the row count as unchanged when evidence is identical

