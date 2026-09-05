## Why

Basins package publication currently accepts `.sp.rivseg` rows without proving that each `iRiv` names an actual `.sp.riv` reach, so a collapsed or misbound HHE mapping can enter immutable package identity and object storage. PR #1875 demonstrated the defect but mixed the fix with unrelated scheduler, autopipeline, calibration, and frontend changes and cannot be merged as one unit.

## What Changes

- Validate the canonical `.sp.riv` / `.sp.rivseg` pair before either Basins source identity or object-store publication is produced.
- Parse bounded, descriptor-verified snapshots of the declared reach and segment blocks; use those same validated bytes for identity and publication.
- Accept actual non-contiguous reach indices and standard `.sp.riv` trailing blocks; reject missing reach references and multi-reach mappings collapsed onto one reach with stable domain errors.
- Prove invalid input creates no environment-backed store root, package lock, object, manifest, or output receipt while valid package bytes and existing consumers remain compatible.
- Add one focused publication-test owner and extend the existing selector/helper routes without weakening the six-owner production facade or the frozen #1912/#1913 baseline corpora.
- Preserve all checked-in calibration overrides; scheduler evidence, autopipeline, frontend, DB crosswalk, and runtime package rewrites remain outside this change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `shud-model-package-publication`: add fail-closed river-segment reach-mapping validation before immutable package identity or publication.
- `orchestrator-structural-burndown`: extend the publication-test partition from six to seven collectible owners while preserving every frozen baseline test and the existing six-file production facade.

## Impact

- Production: `workers/model_registry/basins_package.py` plus existing package leaf owners only; no seventh production owner or package schema change.
- Tests and fixtures: one new rivseg publication suite, the publication selector/helper authority, three synthetic mapping writers, and controlled-transition evidence for frozen fixture/oracle bytes.
- Documentation: current publication-validation commands list the new focused suite; active delta only, with no direct edit to stable specs before archive.
- Operations: no DB migration, runtime package rewrite, Slurm action, production Basins write, or calibration change; focused node-27 and disposable DB-free node-22 evidence is required.
