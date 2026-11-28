# published-pipeline-job-provenance Specification

## Purpose
The compute side has no database, so the jobs that produced a forecast run (their Slurm identity, lifecycle and verified logs) reach the display database only as a published sidecar next to the run. This capability defines that sidecar and its verified task logs on the source side, and the node-27 projection of it into `ops.pipeline_job`: idempotent, monotonic, fail-closed on identity conflicts, independent of scientific re-ingest, and cheap when nothing changed — a run whose sidecar bytes and projected rows are unchanged is skipped without a transaction.

## Requirements

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

A discovered pass that is given an import-state file MAY skip a run, opening no transaction for it, only when all of the following hold: the state file carries a marker for the run; the SHA-256 of the sidecar bytes read in this pass equals the marker's digest; and every job the marker lists is present in `ops.pipeline_job` with the row fingerprint the marker records, a digest over the columns the importer reads and writes. A marker SHALL be written only for a run whose import committed in this pass, SHALL carry the digest of the bytes that import parsed and, for every job of that sidecar (inserted, updated, unchanged or ignored as stale), the fingerprint of its row as read back from the database after the import committed, and SHALL be removed for a run that failed, is unavailable, or is no longer requested. A state file that is missing, unreadable or of an unexpected schema, and a presence read that fails, SHALL be treated as no markers. A sidecar that cannot be read SHALL fail only its own run. Skipped runs SHALL be reported as a count, not as per-run entries. The explicit bounded import and the backfill CLI SHALL NOT read or write the state file.

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

#### Scenario: An unchanged run is skipped without a transaction

- **WHEN** a discovered pass with a state file requests a run that an earlier pass imported, the sidecar bytes are the same and its job rows are still present
- **THEN** no transaction is opened for that run and no statement other than the pass's presence read mentions its jobs
- **AND** the pass summary counts it in `skipped_unchanged` and does not list it in `runs`

#### Scenario: A changed sidecar is imported again

- **WHEN** the sidecar bytes of a marked run differ from the marker's digest
- **THEN** the run goes through the ordinary import and the marker is replaced with the new digest and job ids after it commits

#### Scenario: A failed import is retried

- **WHEN** a run's import fails in one pass
- **THEN** the run has no marker afterwards and the next pass imports it again

#### Scenario: Lost or replaced rows are re-imported

- **WHEN** a marked run's sidecar is unchanged but one of its marked jobs is absent from `ops.pipeline_job`, or present with any importer-visible column different from what the marker's fingerprint records (for example a restored row that lacks a later `log_uri` enrichment at an unchanged `updated_at`)
- **THEN** the run goes through the ordinary import in that pass

#### Scenario: A shared cycle-scoped row is restored by an older sidecar

- **WHEN** two runs of one cycle carry the same cycle-scoped job, the run with the newer copy is no longer requested, and the shared row is lost
- **THEN** the remaining run is imported again and the row is re-inserted from its copy

#### Scenario: An unreadable sidecar fails only its own run

- **WHEN** one requested run's sidecar is a symlink, oversized or otherwise unreadable in a pass with a state file
- **THEN** that run is reported `failed` with the same reason code as without a state file and has no marker
- **AND** the other requested runs are imported or skipped as usual

#### Scenario: Lost or damaged state degrades to a full pass

- **WHEN** the state file is missing, is not valid JSON, or carries another schema
- **THEN** every requested run goes through the ordinary import and a valid state file is written at the end of the pass

#### Scenario: The marker describes the bytes that were imported

- **WHEN** a sidecar is republished while a pass is running
- **THEN** the marker written for that run carries the digest of the bytes the committed import parsed
- **AND** the next pass imports the run again if the published bytes differ from that digest

### Requirement: Backfill without scientific re-ingest

A bounded operator command SHALL explicitly backfill existing runs using the same publisher and importer as the normal lifecycle. It SHALL select only the requested runs, verify their manifests and journal authority, and record a summary that distinguishes publication failures from projection failures. The command SHALL not trigger forcing, parsing, coverage refresh, or publication state changes outside the job read model.

#### Scenario: Backfill a selected completed run

- **WHEN** the operator runs the backfill for one valid completed QHH run
- **THEN** the publisher writes its provenance record and verified log artifacts
- **AND** node-27 imports the resulting rows without re-running the scientific pipeline
- **AND** a later replay reports the row count as unchanged when evidence is identical

### Requirement: Batch provenance publication SHALL replay the journal through one repository
`publish_runs_pipeline_job_provenance` SHALL verify the journal root once and publish every run of the batch through a single `FileOrchestrationJournalRepository` bound to that root, so its stat-identity read cache serves repeated replays of the same cycle journal. Publication content, per-run failure isolation, lock scope and the summary shape SHALL be unchanged. A repository passed to the single-run publisher whose root differs from the verified journal root SHALL be refused with `JOURNAL_REPOSITORY_ROOT_MISMATCH`.

#### Scenario: Same-cycle batch reads the journal from disk about once
- **WHEN** a batch of three or more runs of the same cycle is published and the journal does not change during the batch
- **THEN** the non-cached journal bytes read are about one cycle replay rather than one replay per run, and every run's published jobs equal what a fresh repository would export

#### Scenario: A journal change during the batch is visible to later runs
- **WHEN** a job row for a later run is appended to the cycle journal after an earlier run of the batch was published
- **THEN** the later run's publication includes that row

#### Scenario: A repository bound to another root is refused
- **WHEN** the single-run publisher receives a repository whose root is not the verified journal root
- **THEN** it raises `PipelineJobProvenanceError` with code `JOURNAL_REPOSITORY_ROOT_MISMATCH` and publishes nothing
