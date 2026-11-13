## ADDED Requirements

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
