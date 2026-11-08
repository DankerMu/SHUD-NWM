## Why

The `HttpSlurmGatewayClient` timeout is a hard-coded 60 s (`services/orchestrator/chain_slurm_client.py:77`). #2733 raised it from 30 s, but nothing can configure it.

On node-22 on 2026-11-07, a 65-task `state_save_qc` array submit cost the gateway about 55 s of CPU, and the request body was 17.75 MB. The client gave up at 60 s while Slurm accepted the job every time:
- each of the two `2026-10-15T00Z` cohorts was re-submitted 4 times;
- both cohorts then landed as `permanently_failed`, which stalled the warm-start chain (#3094).

Owner decision (2026-11-07): make the timeout configurable, keep 60 s as the default, and run node-22 at 300 s. Payload reduction is tracked separately.

## What Changes

- **New env `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS`**, read by `OrchestratorConfig.from_env()` into a new field `slurm_gateway_timeout_seconds`.
  - Unset or blank: `DEFAULT_GATEWAY_TIMEOUT_SECONDS`, i.e. 60.0.
  - Any value that is not a finite number greater than 0: `ValueError` naming the variable.
- **Wiring.** `ForecastOrchestrator` passes the field to the `HttpSlurmGatewayClient` it builds. Both `OrchestratorConfig` rebuilds in `scheduler_core._default_orchestrator_for` carry the field.
- **Docs.** `infra/env/compute.scheduler-dbfree.env.example` documents the key and the node-22 value of 300.

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent
Blast radius: a dropped field in a config rebuild silently keeps 60 s (bug persists); a too-permissive parser hides typos; a raising parser stops scheduler passes
Selected risk packs: Config; Legacy compatibility; Error handling; Documentation
Evidence floor: uv run ruff check . ; targeted pytest (tests/test_state_save_submit_ambiguity.py + new config/wiring tests) ; node-22 deployment receipt
```

## Impact

- **Code:**
  - `services/orchestrator/chain_config.py`
  - `services/orchestrator/chain_forecast_orchestrator_cycle.py`
  - `services/orchestrator/scheduler_core.py`
- **Tests:** existing `tests/test_state_save_submit_ambiguity.py`, plus new or extended config and wiring tests.
- **Docs:** `infra/env/compute.scheduler-dbfree.env.example`.
- **Spec:** `slurm-job-chain`.
