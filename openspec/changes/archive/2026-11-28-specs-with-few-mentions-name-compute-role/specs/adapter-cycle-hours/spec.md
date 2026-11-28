## MODIFIED Requirements

### Requirement: Production Runbook Separates Artifact Sources

Production documentation SHALL describe the correct source of truth for forcing,
run outputs, state snapshots, scheduler evidence, and display artifacts.

#### Scenario: Forcing package is checked in shared object-store

- **GIVEN** a published business run references a forcing package
- **WHEN** an operator follows the runbook on the compute control host or node-27
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
