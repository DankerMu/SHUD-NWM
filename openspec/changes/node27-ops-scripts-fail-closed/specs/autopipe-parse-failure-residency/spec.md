## ADDED Requirements

### Requirement: A resident parse-stage failure SHALL raise an operator alert and a self-healing burst SHALL NOT

A node-27 observer SHALL watch every run whose status is `failed`, whatever its error code, that the pipeline is still retrying (last updated within the retry-liveness bound), and SHALL alert (non-zero exit delivered through the unit-failure handler, with the report on the journal as the message body) when at least one run has stayed failing across its observations for at least the residency threshold and has not been alerted within the re-alert interval. On node-27 the output parser is the only writer of the `failed` status, so the watched set carries no error-code filter: the parser's bare output-parsing codes (for example a missing or malformed river output file) are the permanent, never-healing failures the lane exists for. The retry liveness rests on the autopipe re-registering a failed run on every tick, which renews its last-updated time while keeping its status; a repeated parse failure does not rewrite an already-failed run, so the recorded error code is the first failure's and the report SHALL label it as such. A run that recovers before the threshold SHALL NOT cause an alert. Configuration, database, or state errors SHALL exit with a distinct typed status and no traceback.

#### Scenario: A permanent parse failure alerts once per interval

- **GIVEN** a run observed failing with `OUTPUT_PARSE_DB_ERROR` in every observation for longer than the threshold
- **WHEN** the observer runs
- **THEN** it exits 1 with a report naming the run, and a run within the re-alert interval after that exits 0

#### Scenario: A deterministic failure without the output-parse prefix alerts too

- **GIVEN** a run observed failing with `MODEL_RIVER_FILE_MALFORMED` in every observation for at least the threshold
- **WHEN** the observer runs
- **THEN** it exits 1 and the report names the run with `first_error_code=MODEL_RIVER_FILE_MALFORMED`

#### Scenario: The register renews a failed run without rewriting its failure

- **GIVEN** a run already `failed` with a first error code and a last-updated time older than an hour
- **WHEN** the parser fails it again and the autopipe then re-registers it
- **THEN** the repeated failure changes nothing, and the registration keeps status `failed` and the first error code while renewing the last-updated time, so the run stays inside the liveness bound

#### Scenario: A burst that heals within the threshold stays quiet

- **GIVEN** a run observed failing in one or two observations and then parsed
- **WHEN** the observer runs after it has healed
- **THEN** it exits 0, raises no alert, and drops the run from its state
