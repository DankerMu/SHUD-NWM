## REMOVED Requirements

### Requirement: Analysis Run Creation

**Reason**: Analysis runs were created only by `AnalysisOrchestrator`, reachable only through `nhms-pipeline trigger-analysis` on the SQL orchestration path (`PsycopgOrchestratorRepository.from_env`). No production entry point runs it, the DB-free scheduler has no analysis lane, and the SQL path is retired (#3227).
**Migration**: None in production. Warm starts come from forecast-cycle state snapshots. Reintroducing an analysis lane would be a new DB-free capability with its own change.

### Requirement: Analysis Pipeline Job Chain

**Reason**: The orchestrated analysis chain, including the `trigger-analysis` trigger, existed only in `AnalysisOrchestrator` on the retired SQL orchestration path (#3227).
**Migration**: None. The `trigger-analysis` subcommand is removed, and invoking it is an argparse usage error.

### Requirement: Analysis Forcing Selection

**Reason**: Choosing ERA5 or a GFS stand-in for an analysis run's date range was an `AnalysisOrchestrator` decision, and that orchestrator is retired (#3227 R1). The per-valid-time ERA5→GFS fill the forcing producer still performs is specified in "Analysis Forcing Production".
**Migration**: None. No production path selects forcing for an analysis run.

### Requirement: Analysis SHUD Execution

**Reason**: Analysis SHUD jobs were submitted only by the retired `AnalysisOrchestrator` chain (#3227 R1). The SHUD runtime has no analysis-only branch, and Slurm execution, timeout and failure handling for SHUD runs are specified by the forecast pipeline capabilities.
**Migration**: None.

### Requirement: StateSnapshot Generation

**Reason**: This requirement generated state snapshots only from parsed analysis runs, as a stage of the retired analysis chain (#3227 R1). Warm-start state now comes from forecast-cycle snapshots, which `forecast-warm-start` and `file-state-snapshot-index` specify.
**Migration**: None. No analysis run reaches `parsed`, so this path produced nothing.

## MODIFIED Requirements

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
