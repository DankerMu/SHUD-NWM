## Why

Governance-8 completed owner-family extraction behind stable scheduler and
chain facades, but the facade files still retain implementation-sized blocks.
This change continues the burndown by moving remaining implementation blocks
out of `services/orchestrator/scheduler.py` and
`services/orchestrator/chain.py` while preserving legacy imports and
monkeypatch paths until caller migration is explicitly proved.

## What Changes

- Move scheduler Slurm/preflight helper implementation into a focused owner
  module and keep `services.orchestrator.scheduler` private compatibility names
  available for existing tests and callers.
- Continue shrinking scheduler and chain facades by owner-family slices, with
  inventory coverage for every retained compatibility alias.
- Preserve runtime behavior, Slurm submission semantics, DB/schema contracts,
  public API behavior, and production topology.

## Impact

- Affected modules: `services/orchestrator/scheduler.py`,
  `services/orchestrator/scheduler_preflight.py`,
  `services/orchestrator/chain.py`, and future chain owner modules.
- Affected evidence: scheduler/chain compatibility inventories, structural
  line-count evidence, focused scheduler/chain tests, entropy guard checks.
- No compatibility symbol removal in a slice unless its task explicitly
  migrates callers and proves parity.
