# autopipe-parse-failure-residency Specification

## Purpose
Detect a node-27 autopipe hydro run that stays `failed` across retried ticks (the silent permanent rc=1 shape) and alert the operator through the unit-failure mail handler, without alerting on self-healing bursts or abandoned historical failures.

## Requirements

### Requirement: A resident parse-stage failure SHALL raise an operator alert and a self-healing burst SHALL NOT

A node-27 observer SHALL watch every run whose status is `failed`, whatever its error code, that the pipeline is still retrying (last updated within the retry-liveness bound), and SHALL alert (non-zero exit delivered through the unit-failure handler, with the report on the journal as the message body) when at least one run has stayed failing across its observations for at least the residency threshold and has not been alerted within the re-alert interval. On node-27 the output parser is the only writer of the `failed` status, so the watched set carries no error-code filter: the parser's bare output-parsing codes (for example a missing or malformed river output file) are the permanent, never-healing failures the lane exists for. The retry liveness rests on the autopipe re-registering a failed run on every tick, which renews its last-updated time while keeping its status; a repeated parse failure does not rewrite an already-failed run, so the recorded error code is the first failure's and the report SHALL label it as such. The watched set SHALL also include every run with a `PUBLISHED_REPARSE_FAILED` decline (an already `published` run whose rewritten product failed to parse with a deterministic output-parsing code) whose latest such decline lies within the liveness bound and after the run's last successful parse; for these runs the decline time stands in for the last-updated time and the decline detail's leading `<ERROR_CODE>` is the reported code. A run that recovers before the threshold SHALL NOT cause an alert. Configuration, database, or state errors SHALL exit with a distinct typed status and no traceback; a residency threshold that is not shorter than the liveness bound is a configuration error. A watched `failed` run whose recorded forcing version is routed to the legacy timeseries store SHALL be classified `legacy_store_refused`: it alerts on first crossing the threshold like any other run, but SHALL NOT be re-alerted while it stays in the watched set, so a refusal the autopipe deliberately records no decline for cannot produce an unending 24-hour alert.

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

#### Scenario: A published run's deterministic re-parse failure alerts once

- **GIVEN** a `published` run whose rewritten product fails to parse with `MODEL_RIVER_FILE_MALFORMED`
- **WHEN** the autopipe processes it and the observer runs past the threshold
- **THEN** the run stays `published`, a `PUBLISHED_REPARSE_FAILED` decline whose detail starts with `MODEL_RIVER_FILE_MALFORMED: ` exists, the next tick does not retry the same evidence, and the observer exits 1 naming the run and that code; once the decline leaves the liveness bound the run drops out without further mail

#### Scenario: A transient re-parse failure keeps retrying

- **GIVEN** a `published` run whose re-parse fails with `OUTPUT_PARSE_DB_ERROR`, without a parseable code, or with an uncaught traceback on stderr (for example `OSError: [Errno 116] Stale file handle`, or a psycopg error followed by a `DETAIL:` line)
- **WHEN** the autopipe processes it
- **THEN** no decline is written and the run is retried on the next tick

#### Scenario: A legacy-store-refused failed run alerts once and is not re-alerted

- **GIVEN** a `failed` run whose recorded forcing version has `timeseries_store = 'legacy'`, observed failing for longer than the threshold
- **WHEN** the observer runs, and runs again after the re-alert interval with the run still in the watched set
- **THEN** the first run exits 1 with the run reported under `legacy_store_refused`, and the later run exits 0 without mailing it again

### Requirement: A published re-parse blocked by a compressed chunk is declined and visible

When the re-parse of a `published` run fails with `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`, the autopipe SHALL record a `PUBLISHED_REPARSE_FAILED` decline whose detail starts with that code, SHALL leave the run `published`, and SHALL NOT fail the tick for that run again on the same evidence. The residency observer SHALL report the run. `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED` SHALL stay a retried failure. This is the one `OUTPUT_PARSE_`-prefixed code treated as terminal for a published run; the decline clears only by an operator re-run with `--force` or by deleting the decline row, because decompressing the chunk does not change the evidence the decline is keyed on.

#### Scenario: Compressed-chunk block on a published run

- **WHEN** a `published` run's re-parse fails with `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`
- **THEN** the run stays `published`, a `PUBLISHED_REPARSE_FAILED` decline whose detail starts with `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: ` exists, and the next tick does not retry the same evidence or exit non-zero because of it

#### Scenario: Guard failure keeps retrying

- **WHEN** a `published` run's re-parse fails with `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED`
- **THEN** no decline is written and the run is retried on the next tick
