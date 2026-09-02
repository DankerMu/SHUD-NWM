## ADDED Requirements

### Requirement: Autopipeline connections MUST carry bounded connect and statement timeouts

Every database connection opened by `scripts/node27_autopipeline.py` SHALL be opened through the single `_connect` helper, which SHALL apply a connect timeout of 10 seconds (omitted when the DSN query string carries `connect_timeout`, in which case the DSN value wins) and a statement timeout of 600 000 ms (Python-caller override only; the DSN has no path for it); the frontier statistics guard SHALL keep its own `STATS_GUARD_TIMEOUT_MS` budget.

#### Scenario: Default connection

- **WHEN** a tick opens a connection without explicit timeout arguments
- **THEN** the driver receives `connect_timeout=10` and a statement timeout of 600 000 ms effective before the first business statement

#### Scenario: Operator DSN keeps precedence

- **WHEN** the DSN carries `?connect_timeout=3`
- **THEN** `_connect` passes no `connect_timeout` keyword and the backend uses 3 seconds

#### Scenario: A runaway statement cancels instead of wedging

- **WHEN** a statement on a non-guard `_connect` connection exceeds the budget
- **THEN** the driver raises `QueryCanceled` instead of the tick wedging under its flock; on the per-run ingest path (`_process_run`) the affected run is marked `failed` and the tick exits non-zero, while on the seed / pre-loop / publish sites (`_basin_seeded`, `_seed_basin`, `_already_ingested_runs`, `_publish_display_runs`) the exception propagates out of `main()` — the tick exits non-zero with a traceback and no JSON summary — and in both cases the next scheduled tick runs normally

#### Scenario: Guard-leg cancellation keeps the #1643 semantics

- **WHEN** a per-relation `ANALYZE` on a stats-guard connection exceeds `STATS_GUARD_TIMEOUT_MS`
- **THEN** that relation's entry records `status = failed`, the guard summary stays `completed`, and the tick's exit code is unchanged
- **AND** when the guard's connect or candidate query is the statement cancelled, the guard summary records `status = failed` and the tick's exit code is still unchanged

#### Scenario: Stats-guard connection keeps its budget

- **WHEN** the statistics guard opens its connection
- **THEN** its statement timeout equals `STATS_GUARD_TIMEOUT_MS` and its observation semantics are unchanged

### Requirement: The stats-guard disable flag MUST accept the conventional falsy set

`NODE27_AUTOPIPE_STATS_GUARD` SHALL disable the guard when its value, stripped and lower-cased, is one of `0`, `false`, `no`, `off`; any other value keeps the guard enabled.

#### Scenario: Falsy values disable

- **WHEN** the variable is ` FALSE `, `0`, `no` or `Off`
- **THEN** the guard does not run and the receipt records it as disabled

#### Scenario: Other values enable

- **WHEN** the variable is `1`, `on`, or unset
- **THEN** the guard runs
