## ADDED Requirements

### Requirement: Model registry store splits behind a stable facade without drift

The repository SHALL split `packages/common/model_registry.py` so that the facade and
every owner module are below 1,000 lines without adding any `.large-file-guard.json`
exclusion. `PsycopgModelRegistryStore` SHALL remain importable from
`packages.common.model_registry` and SHALL expose every attribute it exposed before,
with each method's source unchanged. Class-attribute and module-level monkeypatch
seams SHALL still reach the real call path, source-pin checks SHALL cover the facade
and every `model_registry_*` owner module, and `from_env`, `_transaction`,
`_PsycopgTransaction` and `_attribution_connect_kwargs` SHALL stay in the facade.

#### Scenario: a class-attribute patch still intercepts a mixin method

- **WHEN** a test patches `PsycopgModelRegistryStore._transaction` (or any other
  patched method) and exercises a store method that now lives in a mixin module
- **THEN** the patched attribute is the one the method calls, and breaking the real
  call site turns that test red.

#### Scenario: oversized consumers stay untouched

- **WHEN** the split lands
- **THEN** every oversized non-excluded consumer of the module has zero diff and
  still passes.
