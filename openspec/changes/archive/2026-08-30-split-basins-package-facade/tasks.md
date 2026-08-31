## 1. Baseline Contract

- [x] 1.1 Persist baseline SHA, `basins_package.py` and guard digests/line counts, complete non-dunder facade name/callable-signature/object-identity snapshot, all ten importer paths (six production/script plus four tests), and the finite eight-callable dynamic seam table plus facade object/constant seams.
- [x] 1.2 Record local baseline per suite: package 4 passed; publication 87 passed/1 skipped; registry import 78 passed/18 skipped; reingest 2 passed/4 skipped; production object-store validation 110 passed; scheduler file registry 59 passed. Persist representative valid identity/manifest/package/success and invalid/error/no-output snapshots; treat macOS cleanup warnings as non-assertion noise rather than portable skip evidence.

## 2. Six-Owner Physical Split

- [x] 2.1 Create exactly `basins_package_contracts.py`, `basins_package_inventory.py`, `basins_package_source_io.py`, `basins_package_manifest.py`, and `basins_package_object_store.py`; reduce the historical `basins_package.py` to a sub-1,000-line facade/coordinator, and keep every owner below 1,000 lines.
- [x] 2.2 Move complete responsibility closures without cleanup/refactor; keep contracts leaf-most, prohibit import-time facade back-imports, preserve all public entrypoints/types/constants, and plain re-export baseline private helpers that are not one of the eight callable seams.
- [x] 2.3 Keep each historical callable signature; wrapper/inject only the eight callable seams through their direct/transitive callers. Bind leaves to the same imported `os` module and `LocalObjectStore`/`ObjectStoreError` class objects for attribute patches, and pass patchable facade limit values only at coordinating call sites.

## 3. Compatibility and Oracle Closure

- [x] 3.1 Add under-limit compatibility coverage that compares baseline/post facade names, signatures and stable object identities, permitting only recorded private implementation `__module__` movement.
- [x] 3.2 Bind every finite seam to a biting high-level test: cover all eight callable forwarding edges plus shared `os`, `LocalObjectStore`, `ObjectStoreError`, existing-manifest limit, and forcing-sample limit behavior; prove the moved call path observes facade/object patches rather than only checking attribute presence.
- [x] 3.3 Compare representative valid source/package identity, manifest/package bytes/checksums, success payload/object effects and existing invalid/symlink/TOCTOU/conflict/lock/write failure/cleanup behavior; report every non-mechanical qualification change as a deviation.
- [x] 3.4 Run fresh-process imports for the explicit ten-file importer set and prove no cycle, public signature, error, forcing/calibration, registry/reingest, or scheduler-consumer drift; run `tests/test_select_ci_tests.py` unchanged after all five new owner paths exist.

## 4. Structural and Scope Gates

- [x] 4.1 Prove all six production files are `<1000` lines, `.large-file-guard.json` is byte-identical with no added/removed exclusion, entropy has no new gate-eligible finding, and the normal commit hook accepts the staged change.
- [x] 4.2 Confirm diff contains no #1903 validator/error/test/fixture bytes, object-store validation split, publication/registry corpus split, selector/CI/docs route change, schema/checksum/forcing/calibration behavior refactor, DB/frontend/Slurm/SHUD change, or dependency.

## 5. Evidence Floor

- [x] 5.1 Run `uv run pytest -q tests/test_basins_package.py tests/test_basins_package_publication.py tests/test_basins_registry_import.py tests/test_basins_reingest.py tests/test_production_object_store_validation.py tests/test_publish_scheduler_file_registry.py`; expect no failures and the recorded local per-suite semantics, allowing only existing platform-specific skips.
- [x] 5.2 Run all affected CLI/QHH/import consumer smokes, `uv run pytest -q tests/test_select_ci_tests.py` unchanged with the five new tracked owner paths, and `uv run pytest -q tests/`; expect complete regression success without oracle weakening.
- [x] 5.3 Run `uv run ruff check .`, entropy/large-file audit tests and script, strict single/all OpenSpec validation, changed Markdown lint, and `git diff --check`; expect zero new violations.
- [x] 5.4 On node-27, first capture the same focused suites at pre-split baseline SHA, then run them at the frozen implementation SHA in detached worktrees without changing the active checkout; compare per-suite Linux results and record a durable receipt. Node-22 is not required because no Slurm/SHUD scheduling/runtime behavior changes.
