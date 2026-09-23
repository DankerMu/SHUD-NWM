## ADDED Requirements

### Requirement: A resident parse-stage failure SHALL raise an operator alert and a self-healing burst SHALL NOT

A node-27 observer SHALL watch runs whose status is `failed` with an `OUTPUT_PARSE_` error code and that the pipeline is still retrying (last updated within the retry-liveness bound), and SHALL alert (non-zero exit delivered through the unit-failure handler, with the report on the journal as the message body) when at least one run has stayed failing across its observations for at least the residency threshold and has not been alerted within the re-alert interval. A run that recovers before the threshold SHALL NOT cause an alert. Configuration, database, or state errors SHALL exit with a distinct typed status and no traceback.

#### Scenario: A permanent parse failure alerts once per interval

- **GIVEN** a run observed failing with `OUTPUT_PARSE_DB_ERROR` in every observation for longer than the threshold
- **WHEN** the observer runs
- **THEN** it exits 1 with a report naming the run, and a run within the re-alert interval after that exits 0

#### Scenario: A burst that heals within the threshold stays quiet

- **GIVEN** a run observed failing in one or two observations and then parsed
- **WHEN** the observer runs after it has healed
- **THEN** it exits 0, raises no alert, and drops the run from its state
