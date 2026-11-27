## MODIFIED Requirements

### Requirement: Current production topology is a governed contract

The project SHALL keep current production topology facts consistent across
active runbooks, role-boundary docs, scripts, and verification instructions.

The compute role SHALL be hosted on two dedicated LAN hosts behind node-27:
`w3` (Slurm controller, compute, scheduler timers, Slurm Gateway, scheduler
journal) and `w2` (compute, `workspace/runs`). The host `210.77.77.22` SHALL be
treated as retired from NHMS compute and kept only as a rollback archive.
`node-22` and `node22` SHALL remain the codename of the compute role in
identifiers (script names, unit names, env keys, receipt schema ids and
capability names); an identifier carrying that codename denotes the compute
role, not the retired host. A requirement title is prose, not an identifier.

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

The guard reports on the compute role under each of its names: the codename,
the two host names (`w3`, `w2`) and the role terms. The role terms are "compute
role", "compute control host", "compute host" and "compute node" (singular or
plural, the two words joined by a space or a hyphen, in any letter case) and
计算角色, 计算控制主机, 计算主机, 计算节点.
The local-PostgreSQL check SHALL have a compute-host leg that recognises the two
host names and the role terms. Its other legs SHALL NOT recognise them.
A host name SHALL be matched on the original text as a lower-case whole token
that is not adjacent to an ASCII letter, digit or underscore, is not part of a
dotted name and is not part of a hyphenated longer name, the ssh alias form
`nwm-` plus host name aside. An upper-case spelling,
a domain name, a longer word that contains the letters and a hyphenated
identifier that contains them are not host names.
An English role term SHALL be matched inside one line as a whole token with the
same boundaries: inside an identifier, a dotted name or a path it is not a name
of the role, and a term broken across a line end is not matched. A role term and
host names joined only by the separators that join host names are one name.

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

#### Scenario: The guard reports a database-writer claim made with a role term

- **WHEN** an active script, runbook, or governance entry uses a role term for
  the subject of a current NHMS active database-writer claim or of an
  active-primary claim
- **THEN** the guard reports the same finding it reports for the codename
- **AND** a negative wording that is allowed for the codename is allowed when it
  uses a role term instead
- **AND** a line that holds only a role term opens a claim the way a line that
  holds only the codename does

#### Scenario: The compute-host leg reads role terms

- **WHEN** a clause of an active surface uses a role term, and the same clause
  names a local PostgreSQL
- **THEN** the guard reports what it reports for a host name in that clause,
  with the same denials; the table-row and scenario-title scenarios are the
  only other exceptions, for a role term and for a host name
- **AND** the legs for the retired host's mirror and port do not read role
  terms

#### Scenario: A scenario title that says what is flagged is not a claim

- **WHEN** the text of a line starts with `#### Scenario:` and contains the word
  "flagged" after it, in any scanned file
- **THEN** the guard reports no database-writer finding for that line, whichever
  name of the role it uses, and no compute-host-leg finding
- **AND** the same words without the title prefix are still reported
- **AND** a claim on a neighbouring line is still reported

#### Scenario: A role term inside a longer name is not a name of the role

- **WHEN** a line names a database writer and contains the words of a role term
  only inside an identifier, a dotted name or a path
- **THEN** the guard reports no compute-role finding for it

#### Scenario: A table row about node-27 is not a claim about the compute role

- **WHEN** a line starts with `|` and its first cell names node-27 and no name
  of the compute role
- **THEN** the compute-host leg reads each cell of the row on its own, so a name
  of the role in one cell and node-27's own PostgreSQL in a different cell are
  not a claim
- **AND** a cell that holds both is still reported
- **AND** a row whose first cell is a name of the role, or names neither, is
  read as a whole line

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
