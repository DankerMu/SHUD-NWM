## Design

**Change surface:**
- `OrchestratorConfig`: the field and the `from_env` parser.
- `ForecastOrchestrator.__init__` at `chain_forecast_orchestrator_cycle.py:114`.
- `scheduler_core._default_orchestrator_for`: both rebuilds, at `:472` (slurm-enabled) and `:496` (source switch).

**Must preserve:**
- `HttpSlurmGatewayClient("...").timeout == 60.0` when no timeout is passed. The test `test_gateway_client_default_timeout_outlasts_an_array_submit` stays as it is.
- With the env unset, every existing construction path produces a 60.0 client.
- `OrchestratorConfig(...)` built directly, as test harnesses do, defaults the field to 60.0.
- An injected `slurm_client` is still used verbatim; the timeout is not applied to it.

**Must add / change:**
- **Field.** `slurm_gateway_timeout_seconds: float = DEFAULT_GATEWAY_TIMEOUT_SECONDS`. It is coerced to `float` in `__post_init__`.
- **Strict parser `_env_positive_seconds(name, default)`.**
  - Unset or blank returns the default.
  - Otherwise it calls `float(value.strip())` and requires `math.isfinite(v) and v > 0`.
  - Any failure raises `ValueError(f"{name} must be a finite number of seconds greater than 0.")`.
  - It does not clamp, unlike `poll_interval_seconds`: a typo silently falling back to 60 s re-creates #3094.
- **Rebuilds.** Both `scheduler_core` rebuilds pass `slurm_gateway_timeout_seconds=config.slurm_gateway_timeout_seconds`.
- **Client construction.** `ForecastOrchestrator` builds the client as `HttpSlurmGatewayClient(config.slurm_gateway_url, timeout=config.slurm_gateway_timeout_seconds)`.

**Governing invariant:** the production scheduler's gateway client timeout equals `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS` when that variable is set, and 60 s when it is not.

**Error handling (accepted failure shape):**
- **Precedent.** An invalid value raises at `from_env()`, the same way `_env_flag` already does. There is no preflight pre-check.
- **Warm-start gate** (`scheduler_generation_gate.py:140`): swallows the error, emits `SCHEDULER_WARM_START_ENV_UNREADABLE` (the repr names the variable), and takes the strict path.
- **Cohort execution** (`scheduler_execution.py:421`): re-raises, so the pass fails without mutating any candidate state.
- **Other scheduler call sites.** `scheduler_core.py:716/725/884` also call `from_env()` and raise the same way. This is not a new failure class: `_env_flag` already raises for `NHMS_REQUIRE_FORECAST_WARM_START`.
- **Scope of the raise.** `from_env()` is called only under `services/orchestrator`, never from `apps/`, `workers/` or `scripts/`, so the raise cannot reach unrelated processes.

**Sibling surfaces:**
- **`AnalysisOrchestrator`** (`chain_analysis_orchestrator.py:33,48`) inherits the same `__init__`, so it picks up the timeout automatically.
- **`scheduler_gateway.py:166`**: the health probe keeps its own `_GATEWAY_PROBE_TIMEOUT_SECONDS`. Unchanged.
- **`scripts/backfill_pipeline_job_provenance.py:106`** has its own `--slurm-gateway-url` CLI. It is a non-goal and is unchanged.
- **The cancel orchestrator** goes through the same `_default_orchestrator_for`, so it is covered.
- **The gateway server-side settings** are unrelated.

**Seams under test:**
- **(a) Parser.** `OrchestratorConfig.from_env()`, driven by monkeypatched env. Unset cases use `monkeypatch.delenv(..., raising=False)`.
- **(b) Scheduler rebuilds.** Follow the #1671 pair in `tests/test_production_scheduler.py` (~:37725-37800): `_set_db_free_scheduler_env` plus `CapturingForecastOrchestrator`, env=`300`. Assert `captured["config"].slurm_gateway_timeout_seconds == 300.0` for both the slurm-enabled branch and the source-switch branch.
- **(c) Client construction.** Build `ForecastOrchestrator` directly with `OrchestratorConfig(..., slurm_gateway_timeout_seconds=300)` and the `FakeCycleRepository` from `tests/test_orchestration_chain.py`, then assert `slurm_client.timeout == 300.0`.
- **(d) Injected client.** An injected client is kept: `orchestrator.slurm_client is injected`.

**Required evidence:**
- **Env unset:** `from_env().slurm_gateway_timeout_seconds == 60.0`, and the built client timeout is 60.0.
- **Env `300`:** both `_default_orchestrator_for` branches yield `slurm_client.timeout == 300.0`.
- **Env `" 300 "`** parses to 300.0. **Env `""`** gives 60.0.
- **Env `abc`, `0`, `-5`, `nan` or `inf`:** `from_env()` raises `ValueError` whose message contains `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS`.

**Non-goals:**
- Payload reduction (separate issue).
- The backfill script's client.
- Gateway server-side timing.
- Changing the 60 s default.

**Review focus:**
- Every `OrchestratorConfig` rebuild carries the field. Grep for `OrchestratorConfig(` in `services/`.
- The parser fails closed and does not clamp.
- An injected client is untouched.
