## 1. Scheduler Facade Shrink

- [x] 1.1 Extract scheduler Slurm/preflight implementation owner.
  - Module/Scope: move database-host, storage-root, template, env, SHUD,
    gateway-helper, GRIB-helper, and production Slurm env helper bodies from
    `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_preflight.py`.
  - Stable Facade: keep `services.orchestrator.scheduler` private names
    importable; keep `_slurm_preflight`, `_slurm_gateway_check`,
    `_default_gateway_probe`, and `_slurm_gateway_backend` monkeypatch behavior
    compatible.
  - Inventory/Evidence Update: add scheduler inventory coverage for the retained
    `scheduler-preflight-compat` alias group.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "slurm_gateway or slurm_preflight or grib_env or database_url or database_host"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

## 2. Chain Facade Shrink

- [x] 2.1 Extract chain source-cycle repair owner slice.
  - Module/Scope: move source-cycle repair, retry provenance,
    repaired-stage evidence, sort-key, task identity, and bounded
    candidate-state helper bodies from `services/orchestrator/chain.py` to
    `services/orchestrator/chain_source_cycle.py`.
  - Stable Facade: keep `services.orchestrator.chain` private names importable
    until caller migration is explicitly covered.
  - Inventory/Evidence Update: add chain inventory coverage for the retained
    `chain-source-cycle-repair-facade` alias group.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py -k "source_cycle or retry_provenance or candidate_state or repaired or repair"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.
