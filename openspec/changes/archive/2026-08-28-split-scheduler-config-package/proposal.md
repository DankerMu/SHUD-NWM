## Why

`services/orchestrator/scheduler_config.py` is 1,624 lines and is exempt from the
1,000-line guard. That temporary exception leaves an active production-config
owner outside structural entropy enforcement, so #1872 requires a behavior-neutral
physical split before the next scheduler-config change.

## What Changes

- Replace the single owner module with a `services.orchestrator.scheduler_config`
  package whose responsibility-focused modules are each below 1,000 lines.
- Preserve the existing package import, scheduler-facade re-export, constructor
  signature, dataclass fields, normalization, preflight evidence, private helper
  module attributes used by tests, and monkeypatch-sensitive callbacks.
- Make the existing resolve-call AST guard scan every Python module in the owner
  package so package extraction cannot make the safety oracle silently blind.
- Remove only `services/orchestrator/scheduler_config.py` from the large-file guard
  exclusions and update the scheduler compatibility inventory.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orchestrator-structural-burndown`: require scheduler configuration owner
  extraction to preserve its public and compatibility contracts while every
  resulting owner file remains below the structural line limit.

## Impact

This change touches scheduler configuration module layout, the compatibility
inventory, one structural test seam, and the exact guard exclusion. It changes no
CLI, environment variable, receipt, scheduler behavior, dependency, database,
display, frontend, or Slurm contract. The independent `tests/test_retention.py`
file split and its exclusion are reserved for the second #1872 PR.
