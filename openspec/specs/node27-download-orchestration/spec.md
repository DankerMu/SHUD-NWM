# node27-download-orchestration Specification

## Purpose
Define how the node-27 download job acquires GFS and IFS source cycles and hands them to the scheduler through raw
manifests on the shared object store: which cycles a pass may take, how it chooses one when none is given, and what
a finished cycle looks like to its consumer.

## Requirements

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

### Requirement: Automatic cycle selection re-drives incomplete cycles and serves the latest allowed cycle first
When no explicit cycle time is given, the node-27 download pass SHALL judge a cycle as downloaded for a source only when the raw manifest readiness rule the scheduler applies answers `ready` for that source and cycle: the manifest identity matches and every referenced raw file is a non-empty regular file. A cycle is ready when every selected source is ready for it; a cycle with at least one selected source that is not ready is a not-ready cycle. Over the window from the chain start to the latest allowed cycle, the pass SHALL select the latest allowed cycle when it is a not-ready cycle, otherwise the oldest not-ready cycle, otherwise the latest allowed cycle. The pass summary SHALL list the cycles judged not ready with the status and reason per source, bounded in length. An explicit cycle time SHALL be honoured as before.

#### Scenario: Started-but-incomplete older cycle is selected again
- **GIVEN** three consecutive allowed cycles whose manifests and files are complete for GFS and IFS
- **AND** one raw file of the middle cycle is then removed for IFS
- **WHEN** the newest of the three is the latest allowed cycle and the pass selects automatically
- **THEN** the middle cycle is selected

#### Scenario: Latest allowed cycle is served before older gaps
- **GIVEN** an older cycle that is not ready and a latest allowed cycle that is not ready
- **WHEN** the pass selects automatically
- **THEN** the latest allowed cycle is selected

#### Scenario: Oldest not-ready cycle is served once the latest is ready
- **GIVEN** a ready latest allowed cycle and two older cycles that are not ready
- **WHEN** the pass selects automatically
- **THEN** the older of the two is selected

#### Scenario: A cycle that never gets a manifest does not starve the latest
- **GIVEN** an older cycle with no manifest for one selected source and a latest allowed cycle with no manifest
- **WHEN** the pass selects automatically
- **THEN** the latest allowed cycle is selected

#### Scenario: Complete chain
- **GIVEN** every cycle from the chain start to the latest allowed cycle is ready
- **WHEN** the pass selects automatically
- **THEN** the latest allowed cycle is selected and the summary lists no not-ready cycle

#### Scenario: Not-ready cycles are reported
- **GIVEN** an older cycle that is not ready for one source
- **WHEN** the pass or the preflight-only run completes with automatic selection
- **THEN** its summary names that cycle with the source's readiness status and reason

### Requirement: Each source is downloaded by its own pass under its own delay
The node-27 download job SHALL run one pass per source on the production timer, SHALL exclude concurrent passes per source and not across sources, and SHALL resolve the automatic-selection delay per source: `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_<SOURCE>` when set, otherwise the shared `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS`, otherwise 8 hours. A pass that selects several sources SHALL use the largest delay among them and SHALL hold the lock of every one of them.

#### Scenario: A source's own delay decides its latest allowed cycle
- **WHEN** the GFS delay key is 6, the shared key is 8, and the time is 6 hours and one minute after a cycle
- **THEN** a pass for GFS alone treats that cycle as its latest allowed cycle
- **AND** a pass for IFS alone treats the previous cycle as its latest allowed cycle

#### Scenario: One source's gap does not pin the other source
- **WHEN** an older cycle is not ready for IFS only and the newest cycle allowed for GFS is not yet downloaded
- **THEN** the GFS pass selects that newest cycle
- **AND** the IFS pass selects the older cycle

#### Scenario: A pass with several sources uses the largest delay
- **WHEN** a pass runs for GFS and IFS together with a GFS delay of 6 and a shared delay of 8
- **THEN** its latest allowed cycle is the one the 8 hour delay allows

#### Scenario: An invalid per-source delay stops selection
- **WHEN** the delay key of a selected source is not numeric or is outside 0..72
- **THEN** automatic selection fails the same way it does for an invalid shared key, naming the per-source key
- **AND** an invalid delay key of a source the pass did not select is ignored

#### Scenario: Passes for different sources do not block each other
- **WHEN** a GFS pass holds its lock
- **THEN** an IFS pass configured with the same lock path runs its download
- **AND** a second GFS pass ends as lock-blocked without downloading

#### Scenario: A pass with several sources is excluded by any of them
- **WHEN** a pass selects GFS and IFS and the lock of one of them is held
- **THEN** it ends as lock-blocked without downloading
- **AND** it leaves the lock of the other source free

#### Scenario: The wrapper runs one source when given one
- **WHEN** the download wrapper is started with the argument `GFS`
- **THEN** the runner is started with `--source GFS`
- **AND** the log file and the summary file are the configured ones with `-gfs` before the extension

#### Scenario: The wrapper refuses an unknown source
- **WHEN** the download wrapper is started with an argument that is not a supported source, or with more than one argument
- **THEN** it exits with the blocked return code and does not start the runner

#### Scenario: The wrapper without an argument is unchanged
- **WHEN** the download wrapper is started without an argument and no per-source delay key is set
- **THEN** the runner is started without `--source` and with the configured log and summary paths
- **AND** the pass selects the same cycle, for the same sources, as before this requirement

#### Scenario: The shipped units run one instance per source
- **WHEN** the download units in the repository are read
- **THEN** there is a template service that starts the wrapper with the instance name and a template timer that starts the same instance every 30 minutes
- **AND** there is no download unit that runs both sources
