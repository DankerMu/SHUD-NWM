# Design

## Invariant Matrix

```text
Governing invariant: every OrchestratorConfig that can reach a production Slurm
poll loop carries poll_interval_seconds >= 1.0, so the orchestrator never polls
the Slurm gateway more than once per second.

Source-of-truth identity/contract: OrchestratorConfig.poll_interval_seconds
(services/orchestrator/chain_config.py:83, default 30.0), floored at 1.0 on
every construction path that production can reach.

Surfaces:
- Producers:
  - services/orchestrator/chain_config.py::OrchestratorConfig.from_env (:139-:147)
    -- the only construction path that reads the environment. The floor moves here.
  - services/orchestrator/scheduler_core.py:446 and :470 -- the only two
    production construction sites besides from_env. Both pass
    poll_interval_seconds=config.poll_interval_seconds (:452, :476), where
    `config` came from OrchestratorConfig.from_env() at :444. They therefore
    propagate an already-floored value and never introduce an unfloored one.
- Validators/preflight:
  - chain_config.py::__post_init__ (:101-:137) -- holds the floor at :111 today.
    After this change it no longer rewrites poll_interval_seconds; every other
    normalization in that method is untouched.
- Storage/cache/query: none -- OrchestratorConfig is a frozen in-memory
  dataclass; poll_interval_seconds is never persisted, serialized into an
  artifact, or read back from one.
- Public routes/entrypoints: none reach this field directly. The orchestrator is
  driven through scheduler_core, covered under Producers.
- Frontend/downstream consumers:
  - services/orchestrator/chain_stage_execution.py:1026
    -- time.sleep(orchestrator.config.poll_interval_seconds), the 844s site.
  - services/orchestrator/chain_forecast_execution.py:1219
    -- time.sleep(self.config.poll_interval_seconds), the 6s site.
  - services/orchestrator/chain_forecast_orchestrator_cycle.py:222, :230
    -- sleep a separate `backoff_seconds`, NOT poll_interval_seconds; measured
    at 0.00s. Out of scope, listed so the audit is complete.
- Failure paths/rollback/stale state:
  - chain_stage_execution.py:1014 -- the poll loop's deadline is
    time.monotonic() + job_timeout_seconds, and the loop exits on a terminal
    job status. With poll_interval_seconds == 0 the loop busy-spins against the
    gateway until one of those two fires; that is precisely the production
    behavior the floor prevents and the reason it is retained on the env path.
- Evidence/audit/readiness: none -- poll_interval_seconds appears in no
  manifest, receipt, or QC payload.
- Deliberately excluded, named so the audit is complete:
  services/slurm_gateway/config.py:58's `sacct_poll_interval_seconds`
  (pydantic Field(default=30, ge=1)) is a different field name on a different
  service tier -- the gateway server's own sacct polling, not the orchestrator
  client's polling of the gateway. It enforces its own minimum through `ge=1`
  and is unreachable from OrchestratorConfig.

Regression rows:
- from_env() with ORCHESTRATOR_POLL_INTERVAL_SECONDS unset -> 30.0
- from_env() with ORCHESTRATOR_POLL_INTERVAL_SECONDS="0" -> 1.0 (floored)
- from_env() with ORCHESTRATOR_POLL_INTERVAL_SECONDS="0.001" -> 1.0 (floored)
- from_env() with ORCHESTRATOR_POLL_INTERVAL_SECONDS="5" -> 5.0 (not raised;
  the floor is a minimum, never a rewrite of a legitimate larger value)
- from_env() with ORCHESTRATOR_POLL_INTERVAL_SECONDS="-3" -> 1.0 (floored;
  a negative must not become a negative sleep)
- scheduler_core._default_orchestrator_for, slurm_execution_enabled branch
  (:446) with ORCHESTRATOR_POLL_INTERVAL_SECONDS="0" -> the reconstructed
  config still carries 1.0
- scheduler_core._default_orchestrator_for, source_id-mismatch branch (:470)
  with ORCHESTRATOR_POLL_INTERVAL_SECONDS="0" -> still 1.0
- direct OrchestratorConfig(poll_interval_seconds=0) -> 0.0. This is the
  deliberate new behavior; before this change it silently became 1.0.
- unchanged sibling consumer: GFSAdapterConfig(poll_interval_seconds=0)
  (workers/data_adapters/gfs_adapter.py:323) -> unchanged, different class
- unchanged sibling consumer: IFSAdapterConfig(poll_interval_seconds=0)
  (workers/data_adapters/ifs_adapter.py:207) -> unchanged, different class
- unchanged sibling consumer: services/production_closure/slurm_validation.py's
  own poll_interval_seconds (:274, consumed at :1084) -> unchanged, different
  class with its own DEFAULT_POLL_INTERVAL_SECONDS and option parsing
```

## Boundary-surface checklist

- **Shared helper roots**: `OrchestratorConfig` is the shared helper. Its other
  eleven `__post_init__` normalizations (workspace_root, object_store_root,
  source_id, forecast_warm_start_required_from, scenario_id/scenario_id_explicit,
  terminal_stage, templates_dir, slurm_job_type_templates, slurm_env,
  target_python_runtime, reconcile_slurm_user/account) must be byte-identical
  after the change. Only the `poll_interval_seconds` line moves.
- **Public entrypoints**: none consume this field directly.
- **Read surfaces**: the two `time.sleep` sites above.
- **Write/delete/overwrite surfaces**: none.
- **Staging/publish/rollback surfaces**: none.
- **Producer/consumer evidence boundaries**: none -- the field reaches no
  evidence artifact.
- **Stale-state/idempotency boundaries**: `OrchestratorConfig` is frozen and
  reconstructed per call in `scheduler_core`; there is no cached instance that
  could retain a pre-change value across the deploy.
- **Unchanged downstream consumers**: the two adapter configs and the
  production-closure validation config, all named in the matrix above.

## Decisions

### D1: floor on the env path rather than a floor field or an injectable sleep

Maintainer decision (recorded on this change): move the floor into `from_env`.

The alternative designs and their costs are set out in `proposal.md`. What makes
this one safe rather than merely small is a fact about the call graph, not an
intention: `from_env` is the **only** construction path that reads the
environment, and the two other production construction sites
(`scheduler_core.py:446`, `:470`) copy `config.poll_interval_seconds` from a
`from_env()` result rather than re-deriving it. So after the move, every value
that can reach the production poll loop has passed the floor exactly once.

The residual risk is explicit and stated in the spec delta: a future direct
construction on a production path would not be floored. Two things bound it —
the spec delta makes the requirement a written contract rather than a line of
code someone can move again, and the regression rows above pin both
`scheduler_core` branches, so a refactor that stops propagating the floored
value fails a test rather than shipping.

### D2: the floor keeps `max`, not a raise-on-invalid

`from_env` reads a free-form environment string. Raising on a too-small value
would turn a mis-set env var into a crash at orchestrator start, which is worse
operationally than silently polling at the safe minimum. The existing behavior
(clamp up, never reject) is preserved verbatim; only its location changes.

### D3: no test file is edited

The eleven files already pass `poll_interval_seconds=0`. This is what makes the
change auditable: if any test's behavior changes beyond getting faster, that is
a real finding, because the only semantic difference is that an argument the
tests already wrote now takes effect. Any test that *fails* after the change was
depending on a 1-second real sleep, which is itself a defect worth surfacing
rather than hiding.

### D4: `timeout-minutes` is not touched here

Maintainer decision. The measured post-fix duration from this change is the
input to that decision, so making both moves in one PR would mean setting the
new number before the measurement that justifies it exists. Issue #1671's
acceptance criterion 4 is conditional ("若采用加时") and stays open.

### D5: what the acceptance receipt must be

Issue #1671's acceptance criterion 1 requires the full lane to reach a **natural
pytest terminal state** (`===== N passed ... =====`) inside the configured
bound, with the measured duration recorded. `unit-test` runs on
`workflow_dispatch` (`.github/workflows/ci.yml:206-208`), so the receipt is
obtainable on this branch without merging. That dispatch run is the oracle for
this change; local timings are supporting evidence, not the receipt.

The pre-existing #1707 red in `tests/test_entropy_audit_script.py` will still
fail in that run. The criterion is a natural terminal state and a measured
duration, not a green suite; the receipt must state this explicitly so the
failure is not mistaken for a regression from this change.
