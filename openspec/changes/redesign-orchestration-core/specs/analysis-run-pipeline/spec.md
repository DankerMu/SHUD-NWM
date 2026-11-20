## REMOVED Requirements

### Requirement: Analysis Run Creation

**Reason**: Analysis runs were created only by `AnalysisOrchestrator`, reachable only through `nhms-pipeline trigger-analysis` on the SQL orchestration path (`PsycopgOrchestratorRepository.from_env`). No production entry point runs it, the DB-free scheduler has no analysis lane, and the SQL path is retired (#3227).
**Migration**: None in production. Warm starts come from forecast-cycle state snapshots. Reintroducing an analysis lane would be a new DB-free capability with its own change.

### Requirement: Analysis Pipeline Job Chain

**Reason**: The orchestrated analysis chain, including the `trigger-analysis` trigger, existed only in `AnalysisOrchestrator` on the retired SQL orchestration path (#3227).
**Migration**: None. The `trigger-analysis` subcommand is removed, and invoking it is an argparse usage error.
