## ADDED Requirements

### Requirement: Gateway reconcile tests remain complete under physical partitioning

The repository SHALL partition the gateway-reconcile test corpus into collectible modules whose
individual line counts do not exceed the configured 1,000-line guard, without adding a guard
exemption or retaining a collectible compatibility shim. Every pre-partition pytest case SHALL be
collected exactly once after partitioning: the module prefix may change, but the complete
`::test_name[param-id]` suffix set, test bodies, decorators, fixtures, and assertion oracles MUST
remain equivalent.

#### Scenario: Collection identity is preserved one-to-one

- **WHEN** the pre-partition and post-partition suites are collected with pytest
- **THEN** both yield 538 unique case suffixes after the first `::`, their sorted suffix sets are byte-identical, and every old suffix maps to exactly one new full node ID

#### Scenario: Every new module satisfies the guard

- **WHEN** line counts are measured for every new collectible and non-collectible gateway-reconcile module
- **THEN** each count is at most 1,000 and `.large-file-guard.json` contains no new exemption

#### Scenario: The complete split suite preserves all oracles

- **WHEN** pytest runs all `tests/test_gateway_reconcile_*.py` modules together
- **THEN** all 538 cases pass without changed assertions, skipped cases, duplicate collection, or production-source modifications

### Requirement: Targeted CI preserves gateway-reconcile ownership after partitioning

The targeted-test selector SHALL replace the deleted monolithic target in the existing
`services/slurm_gateway/**` rule with every collectible gateway-reconcile partition. It SHALL
preserve the prior selection boundary: the partitions remain selected for Slurm-gateway and
real-backend changes, remain excluded from `services/orchestrator/reconcile.py` and
`services/orchestrator/persistence.py` selections under the recorded runtime budget, and
support-module paths select collectible consumers rather than themselves.

#### Scenario: Slurm gateway changes select every partition

- **WHEN** targeted selection runs for a representative `services/slurm_gateway/**` path or `services/slurm_gateway/real_backend.py`
- **THEN** every gateway-reconcile partition is present, the deleted monolith is absent, and all previously selected non-monolith targets remain present

#### Scenario: Runtime-budget exclusions remain narrow

- **WHEN** targeted selection runs for `services/orchestrator/reconcile.py` or `services/orchestrator/persistence.py`
- **THEN** no gateway-reconcile partition is selected and each derived importer gap remains dispositioned as `runtime-budget`

#### Scenario: Support-module changes execute assertions

- **WHEN** a gateway-reconcile support module is the changed path
- **THEN** the selector returns its routed collectible consumer suites plus `tests/test_select_ci_tests.py`, never the support module itself, and does not add the oversized production-scheduler suite

### Requirement: Moved gateway-reconcile dependencies retain their owning context

Shared helpers, monkeypatch targets, source-inspection tests, import-order-sensitive imports, checked-in imports, inventories, and runbook node IDs SHALL point to the new owning modules without changing tested production behavior.

#### Scenario: Runtime-bound helper tests keep their module identity

- **WHEN** the idempotency and file-submit barrier tests run after helper extraction
- **THEN** monkeypatches resolve the module owning `Session`, `_StoreRepo`, and the worker, each source-inspection test reads the file defining its target function, and no re-export shim is required

#### Scenario: Compatibility consumers and documentation resolve moved tests

- **WHEN** selector, entropy, demotion, production-scheduler, active OpenSpec, compatibility-inventory,
  canonical owner-reference, and failed-basin runbook checks run
- **THEN** every imported helper, executable command path, current owner reference, and documented
  node ID resolves to a tracked new module and the stale accepted-decision comment names both
  `absence_retry_permitted` and `operator_verified_absence`
