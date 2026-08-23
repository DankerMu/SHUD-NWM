## Why

`services/orchestrator/scheduler_discovery.py` is already 1,049 lines on the
latest master, so adding more scheduler behavior trips the deterministic
1,000-line source-file guard. A behavior-neutral extraction is needed first,
but naïvely moving these helpers changes which module globals existing callers
and tests can monkeypatch.

## What Changes

- Move the pure implementations of source-discovery evidence, redaction,
  horizon, cycle-hour filtering, and bounded window discovery into a focused
  `scheduler_discovery_evidence` module.
- Keep the historical `scheduler_discovery` names and signatures. Composite
  helpers continue resolving sibling helpers from that owner module, while
  dependency-bearing leaf wrappers inject the owner's current globals at call
  time.
- Add a committed mutation matrix which proves every moved dependency observes
  owner-module replacement rather than a captured or extracted-module binding.
- Preserve default evidence/output/error behavior, historical scheduler facade
  aliases and downstream import seams.
- Keep both source modules within the existing 1,000-line limit without a guard
  exemption.

## Capabilities

### New Capabilities

- `scheduler-discovery-evidence-owner-compat`: Defines the observable owner,
  facade, output, error, and resource-limit compatibility contract for the
  behavior-neutral extraction.

### Modified Capabilities

None. Existing cycle selection, source availability, and redaction product
requirements remain unchanged and are must-preserve oracles for this refactor.

## Impact

- Runtime: `services/orchestrator/scheduler_discovery.py` and new
  `services/orchestrator/scheduler_discovery_evidence.py`.
- Compatibility consumers (expected unchanged):
  `services/orchestrator/scheduler.py`, `scheduler_candidates.py`,
  `scheduler_candidate_runtime.py`, `scheduler_compat_runtime.py`,
  `scheduler_runtime.py`, `scheduler_backfill_predecessor.py`,
  `scheduler_core.py`, and `scheduler_models.py`.
- Tests: source-discovery compatibility and behavior coverage in
  `tests/test_production_scheduler.py`, plus adjacent scheduler regression.
- No database, journal, Slurm, SHUD, display, API, schema, configuration, or
  deployment behavior changes; no node-22 or node-27 receipt is required.
