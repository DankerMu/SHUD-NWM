## ADDED Requirements

### Requirement: Node-22 compute is DB-free after download migration

The system SHALL keep node-22 as a Slurm/SHUD compute and artifact producer
after node-27 owns source download and active source-cycle state.

#### Scenario: Slurm jobs do not inherit business database credentials

- **WHEN** node-22 renders or runs production Slurm jobs after the migration
- **THEN** the sbatch text and process environment do not contain business
  `DATABASE_URL`
- **AND** required inputs are passed as artifact/object-store/workspace
  identities rather than DB credentials
- **AND** DB-mutating stages run on node-27 or are represented by object-store
  receipts that node-27 applies.

#### Scenario: Node-22 scheduler is not the production state owner

- **WHEN** node-27 orchestration submits compute through node-22 Slurm Gateway
- **THEN** node-27 stores pipeline job state and display readiness state
- **AND** node-22 only returns Slurm job ids, job state, logs, and artifact
  receipt locations.

### Requirement: Historical node-22 PostgreSQL is retired with evidence

The system SHALL retire node-22 local PostgreSQL `:55433` after node-27 download
and orchestration prove production readiness.

#### Scenario: Retirement is gated by live evidence

- **WHEN** an operator stops node-22 historical PostgreSQL
- **THEN** an archive/dump path and checksum have been recorded
- **AND** at least two live production cycles covering GFS and IFS have advanced
  through node-27 download, node-22 compute artifacts, node-27 ingest, and
  public display readiness
- **AND** a rollback note identifies the emergency restore path.

#### Scenario: Guardrails block node-22 DB drift

- **WHEN** active env templates, scripts, runbooks, or verification instructions
  reintroduce node-22 `:55433` or `10.0.2.100:55433` as active business DB state
- **THEN** static topology guardrails report a failure unless the reference is
  clearly historical, archived, or compatibility-only with sunset wording
- **AND** current compute role examples do not require a business
  `DATABASE_URL`.

