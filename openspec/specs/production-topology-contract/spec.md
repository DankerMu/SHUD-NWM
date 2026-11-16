# production-topology-contract Specification

## Purpose
TBD - created by archiving change stabilize-data-compute-plane-handoff. Update Purpose after archive.

## Requirements

### Requirement: Current production topology is a governed contract

The project SHALL keep current production topology facts consistent across
active runbooks, role-boundary docs, scripts, and verification instructions.

#### Scenario: Active docs describe the current split

- **WHEN** an operator follows current production docs
- **THEN** node-22 is described as compute/Slurm/artifact producer only
- **AND** node-27 is described as active DB, ingest writer, display API, and
  frontend host
- **AND** historical node-22 DB or node-22 writer material is clearly marked as
  historical or out of current NHMS production topology

#### Scenario: Node-22 local PostgreSQL is marked do-not-connect

- **WHEN** active docs, scripts, env templates, or verification instructions
  mention node-22 local PostgreSQL, port `:55433`, or an archived node-22
  rollback DB mirror
- **THEN** node-22 local PostgreSQL MUST be marked historical, do-not-connect
  for current NHMS production state, archived/stopped rollback-only, and
  sunset-bound
- **AND** any archived rollback mirror reference MUST state explicit DSN only,
  archived-rollback allow flag, compatibility-only purpose, and removal/sunset
  condition

#### Scenario: Verification routes to the correct oracle

- **WHEN** a change modifies ingest, display, DB writes, object-store display
  readiness, or frontend live behavior
- **THEN** validation instructions route live DB/display evidence to node-27
- **AND** validation instructions route Slurm scheduling behavior to node-22
- **AND** local-only checks are not presented as substitutes for required
  node-27 live receipts

### Requirement: Static guardrails block topology drift

The project SHALL provide automated or scripted checks that flag active
node-22-writer and display-env-writer drift in current operational surfaces.

#### Scenario: Active node-22 DB writer assumptions are flagged

- **WHEN** an active script, runbook, or governance entry describes node-22 as
  the current NHMS active DB writer or asks operators to use node-22 local
  PostgreSQL as current production state
- **THEN** the guard reports a finding unless the reference is clearly
  historical, archived, or compatibility-only context

#### Scenario: Display env is not reused for data-plane writer jobs

- **WHEN** active scripts or runbooks configure data-plane writer or archived
  rollback mirror jobs
- **THEN** the guard reports a finding if they source `infra/env/display.env` as
  the writer/mirror authority
- **AND** display API restart and readonly object-store reads may still use
  display runtime env where appropriate

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
