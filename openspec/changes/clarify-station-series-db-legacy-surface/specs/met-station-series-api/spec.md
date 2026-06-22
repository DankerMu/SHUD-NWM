## ADDED Requirements

### Requirement: DB-backed station series helper is legacy internal surface

`PsycopgForecastStore.station_series()` SHALL be treated as a legacy/internal
DB-backed helper retained for historical contract tests and future #631 design
work. The current public display route SHALL NOT call it.

#### Scenario: production code does not call legacy helper

- **WHEN** production Python code under `apps/`, `packages/`, `services/`, or
  `workers/` is inspected
- **THEN** no production call site outside `packages/common/forecast_store.py`
  SHALL invoke `.station_series(`
- **AND** the public FastAPI station-series route SHALL continue to call the
  object-store reader instead of the DB-backed helper

#### Scenario: legacy helper remains explicit until historical API decision

- **WHEN** tests exercise `PsycopgForecastStore.station_series()`
- **THEN** that coverage SHALL be understood as preserving a legacy/internal DB
  contract, not as evidence that the current public route falls back to DB
  history
- **AND** any reintroduction of DB-backed historical station-series access SHALL
  be designed as #631 follow-up work rather than a silent fallback from the
  direct-disk route
