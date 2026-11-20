## MODIFIED Requirements

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
