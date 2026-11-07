## Risk packs

- **Config / project setup — selected.** New env key, strict parser, and the example env. Covered by parser tests and the example update.
- **Legacy compatibility — selected.** With the env unset the timeout is still 60 s, and so is the direct-construction default. Covered by the default tests and the unchanged #2733 test.
- **Error handling — selected.** Invalid values fail closed with a `ValueError` that names the variable. Covered by a parametrised invalid-value test.
- **Documentation — selected.** Covered by the env example comment.
- **Not selected:**
  - Public API / CLI: no CLI flag is added.
  - File IO: no file IO.
  - Schema: no schema change.
  - Auth: no auth change.
  - Concurrency: no shared state.
  - Resource limits: the timeout value is operator-chosen. This change does not alter payload size, which is a non-goal.
  - Release: no dependency change.

## Evidence Floor

- `uv run ruff check .`
- `uv run pytest -q tests/test_state_save_submit_ambiguity.py tests/test_env_templates.py tests/test_node22_scheduler_env_drift_table.py` plus the new or extended config and wiring tests
- node-27 targeted pytest on the PR head
- node-22 deployment receipt, after owner confirmation:
  - set `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS=300`;
  - manually retry the two `2026-10-15T00Z` `state_save_qc` cohorts;
  - the next pass shows the cohorts `succeeded` with no new `STATE_SAVE_SUBMIT_AMBIGUOUS`.

## Tasks

- [x] 1.1 Add the field `OrchestratorConfig.slurm_gateway_timeout_seconds` (default `DEFAULT_GATEWAY_TIMEOUT_SECONDS`, float-coerced) and the strict env parser.
- [x] 1.2 Pass the timeout into `HttpSlurmGatewayClient` in `ForecastOrchestrator`. An injected client stays untouched.
- [x] 1.3 Carry the field through both `OrchestratorConfig` rebuilds in `scheduler_core._default_orchestrator_for`.
- [x] 1.4 Tests (design seams a–d):
  - **Parser:** env unset or blank gives 60; `300` and `" 300 "` give 300; `abc`, `0`, `-5`, `nan` and `inf` raise.
  - **Scheduler rebuilds:** with `CapturingForecastOrchestrator`, both branches capture 300.0.
  - **Client construction:** a direct `ForecastOrchestrator` gets `slurm_client.timeout == 300.0`.
  - **Injected client:** it is kept as is.
- [x] 1.5 Document the key in `infra/env/compute.scheduler-dbfree.env.example`:
  - the key is a bare assignment `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS=300`;
  - the rationale goes in full-line comments only, since `tests/test_env_templates.py:207` forbids inline `#` (#2970).
- [ ] 2.1 Ruff, targeted pytest locally, and node-27 targeted pytest.
- [ ] 3.1 node-22 deployment and recovery receipt (post-merge, owner-confirmed).
