# Model Registry Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for
`workers/model_registry/`. Shared vocabulary lives in `openspec/glossary.md`;
use its terms exactly as defined there.

## Required Reading

- `openspec/specs/model-registration/spec.md`
- `openspec/specs/basins-registry-import/spec.md`
- `docs/runbooks/reingest-bootstrapped-basin.md`
- `packages/common/model_registry.py`

This worker turns Basins source trees into immutable model packages and
registry rows. The `nhms-model` console entry is `cli.py:main()`.

## Ownership Boundaries

- Pipeline order and owners: `basins_discovery.py` (inventory of a Basins root)
  -> `basins_package.py` (immutable package publish into the object store) ->
  `basins_registry_import.py` (registry rows in one transaction) ->
  `basins_reingest.py` (discover, publish, and import for one basin).
  `validator.py` checks a SHUD package for required files.
- Several modules are stable import paths that re-export split owner modules:
  `basins_discovery.py` (`basins_discovery_fs.py`),
  `basins_package_source_io.py` (`basins_source_support.py`),
  `basins_registry_import.py` (`basins_registry_support.py`,
  `basins_registry_preflight.py`, `basins_registry_rows.py`,
  `basins_registry_digests.py`), and `qhh_production_bootstrap.py` (the
  `qhh_bootstrap_*.py` modules). Put new behavior in the owner module and keep
  the stable path a re-export so existing importers and monkeypatches still
  resolve.
- `direct_grid_variant_registration.py` registers a built direct-grid variant
  as a new `core.model_instance` row plus its `met.met_station` mirror rows on
  a cursor the caller owns; it does not open its own transaction.
- The read and lifecycle side of the registry (`PsycopgModelRegistryStore`,
  catalog, lifecycle actions) lives in `packages/common/model_registry.py` and
  its `model_registry_*.py` owner modules. Do not duplicate registry queries
  here; workers may import `packages`, never `services` or `apps`.
- Calibration values change only through declared overrides in
  `basins_calibration_overrides.py`, recorded in the package; never clamp or
  rewrite a calibration value silently.

## State And Mutation Fences

- Registry import writes through `basins_registry_import.py:_transaction()`:
  autocommit off, commit on success, rollback on any exception. Row writers in
  `basins_registry_rows.py` borrow that cursor. Conflicting digests fail with
  `BASINS_REGISTRY_CHECKSUM_CONFLICT` instead of overwriting rows.
- Registry import and reingest require policy evidence (`--auth-actor-id`,
  `--auth-role`) resolved through `packages/common/auth_policy.py`; a missing
  or denied decision stops before any database work.
- Package publish is immutable per `(model_id, version)`: object files are
  created with exclusive no-follow opens, a publish lock key guards concurrent
  publishers (`BASINS_PACKAGE_PUBLISH_IN_PROGRESS`), and a source tree that
  changes after planning fails with `BASINS_PACKAGE_SOURCE_IDENTITY_CHANGED`.
  Republishing means a new version, never an in-place edit.
- Errors are typed per stage (`BasinsDiscoveryError`, `BasinsPackageError`,
  `BasinsRegistryImportError`, `BasinsReingestError`,
  `DirectGridVariantRegistrationError`, `QhhProductionBootstrapError`) and
  carry a stable code. Do not let a raw database or filesystem exception escape
  as the CLI result.
- Reingest of a basin first seeded by a custom bootstrap needs the output-row
  flags documented in `docs/runbooks/reingest-bootstrapped-basin.md`.
- The focused suites below use fakes and run locally. Live registry writes are
  validated on node-27 against its real database, per the oracle table in
  root `CLAUDE.md`.

## Focused Verification

Always run the issue-required governance checks after changing this file or
model registry scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For registration, package, import, or reingest changes, run the focused set
(the selector rule for this directory owns a wider set of suites):

```bash
uv run pytest -q tests/test_model_registration.py tests/test_model_registry_basin_versions.py tests/test_model_registry_list_basins.py tests/test_basins_registry_import.py
```
