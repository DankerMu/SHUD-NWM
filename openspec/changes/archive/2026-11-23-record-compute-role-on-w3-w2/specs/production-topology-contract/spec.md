## MODIFIED Requirements

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
