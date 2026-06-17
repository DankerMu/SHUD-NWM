## ADDED Requirements

### Requirement: Adapter Cycle Hours Are Configurable

GFS and IFS adapters SHALL support environment-configured UTC cycle hours while
preserving legacy default discovery when unset.

#### Scenario: GFS env narrows discovery to business cycles

- **GIVEN** `GFS_CYCLE_HOURS_UTC=0,12`
- **WHEN** `GFSAdapterConfig` is constructed and `discover_cycles()` runs for a date
- **THEN** the adapter discovers/probes only UTC `00` and `12`
- **AND** `as_data_source_config().cycle_hours_utc` reports `[0, 12]`

#### Scenario: IFS env narrows discovery to business cycles

- **GIVEN** `IFS_CYCLE_HOURS_UTC=0,12`
- **WHEN** `IFSAdapterConfig` is constructed and `discover_cycles()` runs for a date
- **THEN** the adapter discovers/probes only UTC `00` and `12`
- **AND** `as_data_source_config().cycle_hours_utc` reports `[0, 12]`

#### Scenario: Adapter env normalizes duplicate unordered hours

- **GIVEN** an adapter cycle-hour env value `12,0,12`
- **WHEN** the adapter config is constructed
- **THEN** the configured cycle hours are `(0, 12)`

#### Scenario: Direct adapter config normalizes duplicate unordered hours

- **GIVEN** direct adapter config `cycle_hours_utc=(12, 0, 12)`
- **WHEN** the adapter config is constructed
- **THEN** the configured cycle hours are `(0, 12)`

#### Scenario: Adapter env rejects malformed hours

- **GIVEN** an adapter cycle-hour env value containing an empty token,
  non-integer, negative hour, out-of-range hour, or blank value
- **WHEN** the adapter config is constructed
- **THEN** construction fails fast with a stable `ValueError`

#### Scenario: Direct adapter config rejects non-integer hours

- **GIVEN** direct adapter config `cycle_hours_utc=(True,)`, `("12",)`, or
  `(12.5,)`
- **WHEN** the adapter config is constructed
- **THEN** construction fails fast with a stable `ValueError`

#### Scenario: Adapter env unset preserves legacy default

- **GIVEN** no adapter cycle-hour env is set
- **WHEN** GFS or IFS adapter config is constructed
- **THEN** the configured cycle hours remain `(0, 6, 12, 18)`

### Requirement: Scheduler Hard Gate Remains Authoritative

Adapter cycle-hour configuration SHALL only reduce provider discovery/probe
breadth and SHALL NOT replace the scheduler allowed-cycle hard gate.

#### Scenario: Scheduler rejects adapter-produced disallowed cycle

- **GIVEN** scheduler allowed cycle hours are `0,12`
- **AND** an adapter or test double produces a UTC `06` or `18` discovery
- **WHEN** production scheduler discovery is evaluated
- **THEN** that cycle is recorded as `cycle_hour_not_allowed`
- **AND** it does not reach candidates, readiness, forcing, or Slurm submit

### Requirement: Production Runbook Separates Artifact Sources

Production documentation SHALL describe the correct source of truth for forcing,
run outputs, state snapshots, scheduler evidence, and display artifacts.

#### Scenario: Forcing package is checked in shared object-store

- **GIVEN** a published business run references a forcing package
- **WHEN** an operator follows the runbook on node-22 or node-27
- **THEN** the runbook normalizes the package URI to a shared object-store
  `forcing/...` key
- **AND** it accepts both relative keys and configured-prefix URIs such as
  `s3://nhms-prod/forcing/...`
- **AND** it derives readiness from a non-empty checksum and package object
  existence
- **AND** it does not imply the forcing package lives under `published/`

#### Scenario: Display artifacts remain published-only

- **GIVEN** node-27 serves display artifacts
- **WHEN** an operator follows the runbook
- **THEN** tiles, logs, and display manifests are checked under `published/`
- **AND** full `runs/` and `forcing/` products are checked under object-store

#### Scenario: Strict warm-start failure handling is documented

- **GIVEN** strict warm-start mode is enabled
- **AND** a forecast fails because the exact successor state checkpoint is
  missing or unusable
- **WHEN** an operator follows the runbook
- **THEN** the guidance checks `hydro.state_snapshot.valid_time` against the
  current `cycle_time`
- **AND** it requires `lead_hours=12`, matching source/model, usable flag, and
  a state URI whose object exists
- **AND** it inspects the producer `state_save_qc` by the snapshot's `run_id` or
  previous-cycle `cycle_id`
- **AND** it does not instruct cold-starting the next forecast as the default
  production remedy
