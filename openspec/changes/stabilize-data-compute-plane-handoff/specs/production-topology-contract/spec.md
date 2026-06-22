## ADDED Requirements

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
  mention node-22 local PostgreSQL, port `:55433`, or a transitional node-22 DB
  mirror
- **THEN** node-22 local PostgreSQL MUST be marked historical, do-not-connect
  for current NHMS production state, and pending removal or sunset
- **AND** any transitional mirror reference MUST state explicit DSN only,
  compatibility-only purpose, and removal/sunset condition

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

- **WHEN** active scripts or runbooks configure data-plane writer or transitional
  mirror jobs
- **THEN** the guard reports a finding if they source `infra/env/display.env` as
  the writer/mirror authority
- **AND** display API restart and readonly object-store reads may still use
  display runtime env where appropriate
