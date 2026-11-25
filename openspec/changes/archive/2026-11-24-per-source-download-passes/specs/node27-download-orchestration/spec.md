## ADDED Requirements

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
