## ADDED Requirements

### Requirement: Node-22 scheduler consumes node-27 NFS raw manifests

The system SHALL keep node-22 as the production scheduler/control point after
node-27 owns GFS/IFS source downloads, and SHALL make shared NFS raw manifests
the source acquisition handoff between the two nodes.

#### Scenario: Scheduler validates NFS raw before downstream start

- **WHEN** node-22 scheduler evaluates a production GFS or IFS source cycle
- **THEN** it checks the configured shared NFS object-store for the node-27 raw
  manifest
- **AND** it verifies manifest source, cycle, URI suffix, entry list, and
  referenced local raw files
- **AND** it records the NFS raw-manifest evidence with the candidate state.

#### Scenario: Raw-ready cycle skips node-22 download

- **WHEN** node-22 scheduler finds node-27 NFS raw ready for a cycle whose
  canonical rows are absent
- **THEN** it builds a downstream restart candidate with `restart_stage=convert`
- **AND** it marks fresh ingestion as not required for that candidate
- **AND** it does not submit node-22 `download_source_cycle` for that cycle.

#### Scenario: Raw-ready cycle is staged for compute nodes before submit

- **WHEN** node-22 scheduler is configured to stage node-27 NFS raw inputs
- **AND** it is about to submit a raw-ready downstream restart candidate
- **THEN** it copies the manifest's referenced raw files from shared NFS to the
  configured compute-visible object-store root
- **AND** it copies the raw manifest last
- **AND** it records raw-input staging evidence before calling Slurm.

#### Scenario: Missing required NFS raw blocks fallback download

- **WHEN** the scheduler is configured to require node-27 NFS raw manifests
- **AND** the expected raw manifest is missing, invalid, or references missing
  raw files
- **THEN** the candidate is blocked with NFS raw-manifest evidence
- **AND** the scheduler does not fall back to node-22 download.

### Requirement: Historical node-22 PostgreSQL retirement remains separately gated

The system SHALL treat node-22 local PostgreSQL `:55433` as historical,
do-not-connect, archived/stopped rollback-only state after #837; this node-27
download / NFS handoff change SHALL NOT reintroduce it as an active DB
dependency.

#### Scenario: Retirement stop gate is bounded by archive and DB-free proof

- **WHEN** an operator stops node-22 historical PostgreSQL
- **THEN** an archive/dump path and checksum have been recorded
- **AND** a replacement for scheduler locks, candidate state, job state, and
  required operational receipts has been implemented
- **AND** post-stop evidence shows no node-22 `:55433` listener
- **AND** scheduler runtime evidence remains DB-free with compute API and Slurm
  gateway health still passing
- **AND** a rollback note identifies the emergency restore path.

#### Scenario: Full post-retirement E2E remains separately tracked

- **WHEN** operators claim full post-retirement production handoff closure
- **THEN** live production cycles covering GFS and IFS have advanced through
  node-27 download, node-22 NFS-gated scheduling, downstream compute,
  node-27 ingest, and public display readiness
- **AND** the evidence is recorded under the pending full E2E task rather than
  the historical PostgreSQL stop/archive gate.

#### Scenario: Guardrails block node-22 DB drift

- **WHEN** active env templates, scripts, runbooks, or verification instructions
  reintroduce node-22 `:55433` or `10.0.2.100:55433` as active business DB state
- **THEN** static topology guardrails report a failure unless the reference is
  clearly historical, archived, or compatibility-only with sunset wording
- **AND** current download ownership examples do not instruct node-22 to perform
  production GFS/IFS source downloads when the NFS raw-manifest gate is enabled.
