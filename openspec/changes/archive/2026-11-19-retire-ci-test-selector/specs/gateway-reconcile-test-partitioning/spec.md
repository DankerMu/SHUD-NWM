## REMOVED Requirements

### Requirement: Targeted CI preserves gateway-reconcile ownership after partitioning

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

## MODIFIED Requirements

### Requirement: Moved gateway-reconcile dependencies retain their owning context

Shared helpers, monkeypatch targets, source-inspection tests, import-order-sensitive imports, checked-in imports, inventories, and runbook node IDs SHALL point to the new owning modules without changing tested production behavior.

#### Scenario: Runtime-bound helper tests keep their module identity

- **WHEN** the idempotency and file-submit barrier tests run after helper extraction
- **THEN** monkeypatches resolve the module owning `Session`, `_StoreRepo`, and the worker, each source-inspection test reads the file defining its target function, and no re-export shim is required

#### Scenario: Compatibility consumers and documentation resolve moved tests

- **WHEN** entropy, demotion, production-scheduler, active OpenSpec, compatibility-inventory,
  canonical owner-reference, and failed-basin runbook checks run
- **THEN** every imported helper, executable command path, current owner reference, and documented
  node ID resolves to a tracked new module and the stale accepted-decision comment names both
  `absence_retry_permitted` and `operator_verified_absence`
