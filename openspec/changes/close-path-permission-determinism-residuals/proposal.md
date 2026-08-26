## Why

Four pre-existing path probes still expose Python-version-dependent failures after the `path-expansion-throw-face-closure` family change: two write-side lanes leak errno-less `Path.expanduser()` `RuntimeError`, while scheduler and Basins permission probes disagree between CPython 3.11 and later interpreters because `Path.exists()`/`is_dir()` swallow different `OSError` sets. The four issues share one invariant and one cross-version evidence matrix, so they are delivered together without rewriting the archived predecessor change.

## What Changes

- Convert an undeterminable published-artifact root into the orchestrator's existing `PUBLISHED_LOG_WRITE_FAILED` contract without creating a literal `~user` path.
- Convert an undeterminable production-met evidence root into `PRODUCTION_MET_EVIDENCE_PATH_UNSAFE` at both `ProductionMetConfig.from_env` and `validate_met`.
- Make the scheduler final-component directory guard fail closed with its existing structured `ValueError` family when the resolved target cannot be classified because traversal is denied, identically on CPython 3.11+.
- Replace unguarded Basins root metadata probes with errno-aware classification: missing stays `BASINS_ROOT_NOT_FOUND`, while `EACCES`/`EPERM` becomes `BASINS_ROOT_UNREADABLE` in both discovery and migration-report entrypoints.
- Route permission-denied required-file paths into the existing `unreadable_required_files` third state and `partial` model status instead of mislabelling them as unresolvable symlinks.
- Remove the predecessor's live HONEST LIMIT text and tests only where these issues now supply end-to-end evidence; archived OpenSpec artifacts remain immutable.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `safe-filesystem-primitive-contract`: Extend the write-side structured-error contract to the orchestrator published-artifact and production-met evidence-root wrappers.
- `slurm-array-runner-integration`: Split non-loop strict-resolution fallback from the final directory-classification verdict and make permission denial fail closed across interpreters.
- `basins-asset-discovery`: Classify root and required-file permission failures by their actual semantics instead of Python-version behavior or symlink terminology.

## Impact

Affected code is limited to `chain_runtime_utils.py`, `chain_workspace.py`, `met_validation.py`, `scheduler_runtime_roots.py`, `basins_discovery.py`, `basins_package.py`, their existing focused tests, and the three capability deltas above. There is no database migration, payload-key change, external dependency, node-22 scheduling change, or live display deployment. Issues #1621, #1622, #1623, and #1554 close in one PR.