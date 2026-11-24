# Drop unread imports from the chain facade; rename inventory-named tests

## Why

Two leftovers of the facade cleanup (#3335 items 6 and 7):

- `services/orchestrator/chain.py` still imports names that nothing reads:
  not `chain.py` itself, not an owner module or test through the facade, not
  a string patch target. They were kept so that names stayed resolvable
  while the compatibility tables existed. Candidates measured on master:
  `importlib`, `os`, `UTC`, `_time_normalization_module`,
  `_tile_publisher_module`, `_tile_publisher_publisher_module`,
  `_canonical_converter_module`, `BestAvailableManager`, `persistence`,
  `reservation`, `retry`, `_chain_stages_module`, `_chain_types_module`,
  `SlurmGatewaySettings`.
- Eighteen tests are still named `..._and_inventory` after the compatibility
  inventories deleted in #3274. Their bodies check owner identity, not an
  inventory.

## What Changes

- Each candidate import without a reader is removed from `chain.py`. If a
  removal drops modules from the set loaded by importing the chain or
  scheduler facade, the dropped modules are recorded and checked for
  consumers; an import stays only when a consumer exists, with a comment
  naming it.
- A new test collects every name `chain.py` binds by import and fails on any
  without a reader, so the list above does not have to be trusted and the
  state cannot regress silently (`F401` is exempt file-wide in `chain.py`).
- The eighteen tests lose the `_and_inventory` suffix. Bodies unchanged.

## Behaviour

None for callers. No function body changes. The names removed stop being
attributes of `services.orchestrator.chain`; none has a reader. Importing the
facade may no longer load a module that only an unread import pulled in
(measured: `packages.common.best_available` and, for the chain facade,
`packages.common.forcing_ts_render`; neither registers anything at import
and neither has a consumer of its being loaded). One visible consequence:
the node-27 delegated-connect audit computes each registered component's
static import closure, and components that reached `best_available.py` only
through `chain.py` no longer do. The closure gets smaller, never larger.

## Impact

- Code: `services/orchestrator/chain.py` (import lines only).
- Tests: `tests/test_orchestration_chain.py`, `tests/test_production_scheduler.py`
  (renames); new `tests/test_chain_facade_import_readers.py`;
  `tests/test_node27_connection_attribution_delegated.py` (registry rows
  that the smaller import closure makes stale) and one reason text in
  `tests/test_node27_connection_attribution.py`.
- Spec: `chain-facade-deepening` (one added requirement).
- Operations: `chain.py` is production code; the change is import lines
  only. Compute-node evidence rides the next deploy with the other #3335
  slices.
