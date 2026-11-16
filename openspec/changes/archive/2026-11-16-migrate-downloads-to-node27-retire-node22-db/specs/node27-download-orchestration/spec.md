## ADDED Requirements

### Requirement: Node-27 download runner owns source acquisition handoff

The system SHALL run production GFS/IFS source discovery and raw download on
node-27 under an explicit data-plane download role, separate from the node-27
display_readonly runtime, and SHALL persist raw files plus manifest evidence on
the shared NFS object-store.

#### Scenario: Download preflight validates node-27 writer dependencies

- **WHEN** the node-27 download runner starts
- **THEN** it validates writer `DATABASE_URL`, `OBJECT_STORE_ROOT`,
  `WORKSPACE_ROOT`, GRIB toolchain availability, source cycle-hour config,
  bbox config, lock path, and log/evidence roots before downloading
- **AND** it rejects display_readonly-like credentials and node-22 historical
  PostgreSQL endpoints such as `:55433`
- **AND** failure output redacts credentials and exits before partial DB or
  object-store mutation.

#### Scenario: Download evidence is bounded and credential-safe

- **WHEN** a node-27 download pass completes or fails
- **THEN** it writes a bounded JSON summary that identifies source, cycle,
  status, manifest URI, file count, bytes written, retry count, object-store
  root identity, and database host/port without secrets
- **AND** one source failure does not imply display API failure or readonly
  runtime misconfiguration.

#### Scenario: Node-27 raw cycle state is canonical

- **WHEN** a GFS or IFS source cycle is downloaded in production
- **THEN** raw source-cycle state and manifest identity are written to node-27
  active PostgreSQL
- **AND** node-22 historical PostgreSQL is not read or written for source-cycle
  truth
- **AND** re-running the same cycle is idempotent and does not corrupt existing
  raw manifest identity.

#### Scenario: NFS raw manifest handoff is complete before node-22 scheduling

- **WHEN** a node-27 download completes for a GFS or IFS cycle
- **THEN** the raw manifest exists at the shared NFS object-store path
  corresponding to `raw/<source>/<cycle>/manifest.json`
- **AND** every referenced local raw file exists, is regular, and is non-empty
- **AND** the manifest source, cycle, and URI suffix match the cycle that
  node-22 scheduler will evaluate.

### Requirement: Node-27 production pass drives allowed cycles

The system SHALL provide a bounded production pass that selects allowed GFS/IFS
business cycles on node-27 and hands completed raw cycles to node-22 scheduler
through the shared NFS manifest contract.

#### Scenario: Allowed-cycle selection remains explicit

- **WHEN** the production download pass selects candidate source cycles
- **THEN** it honors the configured allowed UTC cycle hours
- **AND** it records skipped, unavailable, already-complete, failed, and
  downloaded cycles separately for GFS and IFS.

#### Scenario: Public display advances from node-27-downloaded raw cycles

- **WHEN** a new production GFS or IFS cycle is downloaded by node-27
- **THEN** node-22 scheduler observes the raw manifest on NFS before starting
  downstream compute
- **AND** subsequent compute/ingest/display evidence uses the same
  source/cycle/run identity
- **AND** public latest-product endpoints can advance without node-22 performing
  a production download for that cycle.
