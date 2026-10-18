# Forcing Producer Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for
`workers/forcing_producer/`. Shared vocabulary lives in `openspec/glossary.md`;
use its terms exactly as defined there.

## Required Reading

- `docs/runbooks/object-store-forcing-series-read.md`
- `openspec/specs/forcing-production/spec.md`
- `openspec/specs/direct-grid-binding-artifact/spec.md`
- `docs/adr/0006-forcing-csv-out-of-basins-package-identity.md`

This worker turns canonical meteorological products into SHUD station forcing
for one `(source, cycle, model)`. The `nhms-forcing` console entry is
`cli.py:main()`. The display API reads the published forcing CSV straight from
the object store, so the file layout here is a cross-plane read contract.

## Ownership Boundaries

- `producer.py` owns `ForcingProducer` (`from_env`, `produce`), canonical
  product selection, IDW weights, direct-grid station binding, unit
  validation, `.tsd.forc` formatting, and `ForcingProductionError` with its
  subclasses.
- Persistence is a repository chosen in `ForcingProducer.from_env()`:
  `store.py:PsycopgForcingRepository` (database) or
  `file_store.py:FileForcingRepository` (object store only) when
  `file_store.py:db_free_repository_enabled()` is true. Any new repository
  method must exist on both with the same semantics.
- The direct-grid contract parser lives in
  `packages/common/contracts/direct_grid.py`; import it from there. The package
  `__init__.py` still re-exports its public names, but add no logic here.
- Shared forcing contracts stay in `packages/common`:
  `packages/common/shud_forcing_contract.py`,
  `packages/common/forcing_store_routing.py`,
  `packages/common/forcing_domain_handoff.py`,
  `packages/common/grid_signature.py`, and
  `packages/common/grid_registry_bbox_guard.py`. Canonical-product usability
  comes from `workers/canonical_converter/converter.py`; do not fork that rule
  here. Workers may import `packages` and other workers, never `services` or
  `apps`.
- `workers/grid_registry/registry.py` imports producer grid helpers lazily for
  its live-signature cross-check, so a change to grid-point derivation here is
  also a registry change.

## State And Mutation Fences

- Forcing artifacts are written under
  `forcing/<source>/<cycle>/<basin_version_id>/<model_id>/` through
  `LocalObjectStore.write_bytes_atomic`; the package manifest is written last.
  Forcing CSV payloads are outside basins package identity (ADR 0006); do not
  fold them back into package checksums.
- Database forcing writes in
  `store.py:PsycopgForcingRepository.replace_forcing_timeseries` take the
  shared ingest fence (`try_ingest_fence`) and call
  `check_batch_targets_uncompressed` before the delete-and-insert.
  `tests/test_timescale_write_guard_wire_site_invariant.py` checks that wiring
  by AST; keep the hook when refactoring.
- The CLI reports a busy fence as `FORCING_PRODUCE_COMPRESSION_FENCE_BUSY`
  (deferred, retried by the next tick) and compressed-chunk refusals as their
  own codes. Do not collapse them into a generic failure.
- Direct-grid production fails closed in preflight when the registered grid
  snapshot is missing (`MissingRegisteredGridSnapshotError`), superseded
  (`SupersededGridSnapshotError`), or its bbox disagrees with the download
  region (`BboxMismatchError` from the bbox guard). Never invent a snapshot.
- Source identifiers go through `packages/common/source_identity.py`
  normalization at the CLI boundary and in the repositories.

## Focused Verification

Always run the issue-required governance checks after changing this file or
forcing producer scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For producer, repository, CLI, or source-identity changes, run the focused set:

```bash
uv run pytest -q tests/test_forcing_producer.py tests/test_forcing_producer_cli.py tests/test_production_met_validation.py tests/test_source_identity.py
```
