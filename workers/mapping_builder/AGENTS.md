# Mapping Builder Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for
`workers/mapping_builder/`. Shared vocabulary lives in `openspec/glossary.md`;
use its terms exactly as defined there.

## Required Reading

- `openspec/specs/element-grid-ownership-mapping/spec.md`
- `openspec/specs/mapping-input-integrity/spec.md`
- `openspec/specs/mapping-evidence-package/spec.md`
- `openspec/specs/direct-grid-binding-artifact/spec.md`

This worker is the offline producer of direct-grid mapping assets: it reads a
baseline SHUD package and a registered grid snapshot and emits a variant
package, a binding artifact, and an immutable evidence package.

## Ownership Boundaries

- Each gate has one owner module. `integrity.py` owns G0 baseline integrity,
  CRS authority, and ancillary inventory; `algorithm.py` owns the G2 grid
  identity precondition and the versioned nearest-cell mapping;
  `rewrite.py` owns the `.sp.att` FORC rewrite and the G4 gates (byte-level
  non-FORC preservation, hydrologic core identity and fingerprint, legacy
  weather path scan); `binding.py` owns the G5 manifest and station binding
  artifact; `evidence.py` owns the evidence package; `z_policy_verdict.py` is
  the single `z_policy` input authority.
- `cli.py` only chains the library stages and resolves operator input
  authority (`resolve_package_path`). It never re-implements a stage;
  `tests/test_mapping_builder_cli.py` fails when a stage function is defined
  locally instead of imported from its stage module.
- `__init__.py` re-exports the public stage names. Keep it a re-export; new
  logic goes in the owning stage module.
- Grid snapshots arrive through the `GridSnapshotLoader` protocol in
  `algorithm.py`; signatures come from `packages/common/grid_signature.py`.
  The binding contract must stay parseable by
  `packages/common/contracts/direct_grid.py`, the parser the forcing producer
  uses. Workers may import `packages` and other workers, never `services` or
  `apps`.
- `rewrite.py` is imported by `packages/common/state_clone.py` under an entry
  in `tests/import_layering_allowlist.json`. Its public signatures are a
  cross-package contract; do not widen that dependency.

## State And Mutation Fences

- The baseline package is read-only. Gates read it and never repair, rename,
  or rewrite baseline files; classification findings are recorded, not fixed.
- No database access. The mapping stage must not write `met.interp_weight`,
  `met.met_station`, or `met.forcing_version` (`FORBIDDEN_MET_TABLES` in
  `binding.py`); the `DbWriteSpy` contract lets an orchestrator prove that.
  Registration of a built variant belongs to
  `workers/model_registry/direct_grid_variant_registration.py`.
- No partial output. `build_direct_grid_variant` requires a `variant_root` that
  does not exist, stages into a sibling `.building` directory, renames it into
  place on success, and removes the staging tree on any failure.
- An evidence package is immutable once assembled; superseding a variant builds
  a new package. Its checksum is bound to the mapping asset checksum.
- Every gate fails closed with a typed error (for example
  `GridSignatureMismatchError`, `SmallBasinBlockedError`,
  `BindingChecksumMismatchError`) rather than emitting a degraded asset.
- `resolve_package_path` accepts only the release-frozen object-store package
  shape and refuses dev workspace paths unless an explicit override with a
  rationale is recorded in the input authority evidence.

## Focused Verification

Always run the issue-required governance checks after changing this file or
mapping builder scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For any stage, CLI, evidence, or z-policy change, run the mapping builder
suites:

```bash
uv run pytest -q tests/test_mapping_builder_*.py
```
