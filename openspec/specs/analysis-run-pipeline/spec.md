# analysis-run-pipeline Specification

## Purpose
Specify the analysis behaviour that remains in the workers after the orchestrated analysis lane was retired: ERA5 forcing production with its recorded GFS fill, and parsing of output from a run whose `run_type` is `analysis`.

## Requirements

### Requirement: Analysis Forcing Production
When the forcing producer is invoked with `source_id='ERA5'`, it SHALL fill each target valid time whose ERA5 canonical product is missing with the minimum-lead GFS canonical product for that valid time, and it SHALL record the fill in the forcing lineage (`workers/forcing_producer/producer.py`, `ERA5_LATENCY_FALLBACK_REASON`, `ERA5_FALLBACK_SOURCE_ID`). This is worker behaviour keyed on the source id, not on `run_type`. No production orchestrator currently invokes it for an analysis run.

#### Scenario: ERA5 forcing production
- **WHEN** the forcing producer runs with `source_id='ERA5'` and ERA5 canonical products cover every target valid time
- **THEN** it produces the forcing package from those products with no fallback lineage

#### Scenario: ERA5 latency fallback forcing
- **WHEN** the forcing producer runs with `source_id='ERA5'`, ERA5 canonical products are missing for some target valid times, and GFS canonical products exist for them
- **THEN** it fills each missing valid time with the minimum-lead GFS product, and the forcing lineage records `fallback_reason='era5_latency'`, `fallback_source_id='gfs'` and the filled `fallback_valid_times`

### Requirement: Analysis Output Parsing
The output parser SHALL accept a run manifest with `run_type='analysis'` (`workers/output_parser/parser.py`). This is worker behaviour. No production orchestrator currently creates analysis runs, and parse status and failure handling are the same as for every other `run_type`.

#### Scenario: Analysis river_timeseries ingestion
- **WHEN** the parser ingests `.rivqdown` output for a run whose `run_type` is `'analysis'`
- **THEN** it writes `river_timeseries` rows with `lead_time_hours=NULL`, and the run's scenario stays on `hydro_run.scenario_id`, not on the rows
- **AND** rows whose valid times fall outside the run's `start_time`..`end_time` window are kept, not clipped to it

#### Scenario: Analysis parse updates run status
- **WHEN** an `'analysis'` manifest has no `cycle_time` or no `end_time`
- **THEN** the parser does not raise `CYCLE_TIME_MISSING` or `END_TIME_MISSING` (it raises them for every other `run_type`), anchors automatic time-basis detection on the run's `start_time`, and finishes with status `parsed`

#### Scenario: Analysis parse failure
- **WHEN** the `.rivqdown` output of an `'analysis'` run is missing, empty or malformed
- **THEN** the parser fails with the same error codes as for any other `run_type` (`RIVQDOWN_NOT_FOUND`, `RIVQDOWN_EMPTY`, `RIVQDOWN_NO_DATA`, `MALFORMED_ROW`), and the run does not reach `parsed`
