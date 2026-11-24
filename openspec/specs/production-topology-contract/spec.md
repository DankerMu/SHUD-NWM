# production-topology-contract Specification

## Purpose
TBD - created by archiving change stabilize-data-compute-plane-handoff. Update Purpose after archive.

## Requirements

### Requirement: Current production topology is a governed contract

The project SHALL keep current production topology facts consistent across
active runbooks, role-boundary docs, scripts, and verification instructions.

The compute role SHALL be hosted on two dedicated LAN hosts behind node-27:
`w3` (Slurm controller, compute, scheduler timers, Slurm Gateway, scheduler
journal) and `w2` (compute, `workspace/runs`). The host `210.77.77.22` SHALL be
treated as retired from NHMS compute and kept only as a rollback archive.
`node-22` and `node22` SHALL remain the codename of the compute role in
identifiers (script names, unit names, env keys, receipt schema ids, spec and
requirement names); an identifier carrying that codename denotes the compute
role, not the retired host.

#### Scenario: Active docs describe the current split

- **WHEN** an operator follows current production docs
- **THEN** the compute role is described as compute/Slurm/artifact producer only
- **AND** node-27 is described as active DB, ingest writer, display API, and
  frontend host
- **AND** historical node-22 DB or node-22 writer material is clearly marked as
  historical or out of current NHMS production topology

#### Scenario: The compute role names its hosts

- **WHEN** agent instructions, `docs/governance/ROLE_BOUNDARY.md`, or
  `docs/runbooks/current-production-ops.md` state where compute runs
- **THEN** they name `w3` and `w2` as the hosts of the compute role
- **AND** they state that the host `210.77.77.22` is retired from NHMS compute
  and is a rollback archive only
- **AND** they link `docs/runbooks/w2-w3-compute-topology.md` for the host
  layout

#### Scenario: Runbooks that still say node-22 carry a banner

- **WHEN** `docs/runbooks/current-production-ops.md` (which also covers its
  `production-ops/` pages), `docs/runbooks/two-node-deployment-overview.md`, or
  `docs/runbooks/node22-control-plane-manual-recovery.md` is opened
- **THEN** a banner at its top says that "node-22" denotes the compute role
  hosted on `w3` and `w2`
- **AND** the banner says that a literal ssh command to `210.77.77.22` in the
  page is run on `w3` instead and SHALL NOT be run on the retired host

#### Scenario: Compute hosts inherit every compute-role boundary

- **WHEN** a rule in this contract, in agent instructions, or in a current
  runbook restricts node-22 as the compute role (no connection to any active
  NHMS database, no writer role, exact-interpreter execution, changes only at
  a scheduler pass boundary)
- **THEN** the rule applies to `w3` and to `w2`
- **AND** no current doc presents `w3` or `w2` as an NHMS database host, an
  ingest writer, or a display host

#### Scenario: Only the control host runs the production user's units

- **WHEN** agent instructions or the host-layout runbook describe access to
  `w2`
- **THEN** they state that the production user's systemd user manager runs
  on `w3` only, because the home directory holding the user units is shared
  with `w2` over NFS
- **AND** they forbid logging in to `w2` as the production user and enabling
  linger for it there

#### Scenario: Retired-host material keeps its meaning

- **WHEN** a doc or spec speaks of node-22 local PostgreSQL, port `:55433`, or
  the Python 3.12.7 environment of #1831
- **THEN** "node-22" there denotes the retired host `210.77.77.22`, not the
  compute role, and the archived/stopped do-not-connect wording stays

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
- **AND** validation instructions route Slurm scheduling behavior to the
  compute role's control host `w3`
- **AND** local-only checks are not presented as substitutes for required
  node-27 live receipts

### Requirement: Static guardrails block topology drift

The project SHALL provide automated or scripted checks that flag active
compute-role database-writer and display-env-writer drift in current operational
surfaces.

The database-writer check SHALL recognise the compute role by each of its names:
the codename and the two host names (`w3`, `w2`).
The local-PostgreSQL check SHALL have a compute-host leg that recognises the two
host names. Its other legs SHALL NOT recognise the host names.
A host name SHALL be matched on the original text as a lower-case whole token
that is not adjacent to an ASCII letter, digit or underscore, is not part of a
dotted name and is not part of a hyphenated longer name, the ssh alias form
`nwm-` plus host name aside. An upper-case spelling,
a domain name, a longer word that contains the letters and a hyphenated
identifier that contains them are not host names.

#### Scenario: Active node-22 DB writer assumptions are flagged

- **WHEN** an active script, runbook, or governance entry describes node-22 as
  the current NHMS active DB writer or asks operators to use node-22 local
  PostgreSQL as current production state
- **THEN** the guard reports a finding unless the reference is clearly
  historical, archived, or compatibility-only context

#### Scenario: A database-writer claim about a compute host is flagged

- **WHEN** an active script, runbook, or governance entry describes a compute
  host, by host name, as the current NHMS active database writer or as the host
  of the active primary database
- **THEN** the guard reports the same finding it reports for the codename
- **AND** a negative wording that is allowed for the codename is allowed when it
  uses a host name instead, alone or as a pair of both host names

#### Scenario: A compute host's local PostgreSQL is flagged unless denied

- **WHEN** a clause of an active surface names a compute host, by host name,
  together with a local PostgreSQL
- **THEN** the guard reports a finding unless that same clause denies that such
  a database exists or forbids its use; wording that only calls it inactive or
  outside today's topology is not a denial
- **AND** the archived, stopped or rollback wording that excuses a reference to
  the retired host's own PostgreSQL does not excuse it

#### Scenario: Text that only looks like a host name is not flagged

- **WHEN** a line names a database writer and contains a host name's letters
  only in upper case, inside a domain name, or inside a longer word
- **THEN** the guard reports no compute-role finding for it

#### Scenario: Slurm's accounting database is not an NHMS database

- **WHEN** a clause says that slurmdbd or Slurm accounting on a compute host
  writes to its own accounting store, and names no PostgreSQL, primary, NHMS or
  current, active or production state
- **THEN** the guard reports no database-writer finding for it, whichever name
  of the role the clause uses
- **AND** a clause that names slurmdbd and also claims the PostgreSQL or NHMS
  primary for the host is still reported

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
