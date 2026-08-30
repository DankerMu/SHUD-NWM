## Why

Issue #1903 cannot modify the 3,120-line Basins package publisher because the
repository's commit hook rejects any touched non-excluded file above 1,000 lines.
Issue #1910 is the first #1906 child: it isolates the production facade split so
the mapping fix can later land without mixing structural movement with behavior.

## What Changes

- Split `workers/model_registry/basins_package.py` into six finite owners: the
  historical facade plus contracts, inventory/planning, source IO/path safety,
  forcing/manifest material, and object-store operation modules.
- Keep the three public entrypoints and complete historical module attribute surface
  importable from the facade.
- Preserve runtime monkeypatch semantics through facade wrappers that inject the
  current facade-bound dependency into leaf implementation functions.
- Require the facade and every new owner module to remain below 1,000 lines while
  keeping `.large-file-guard.json` byte-identical.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orchestrator-structural-burndown`: require the Basins package compatibility
  facade split to preserve imports, dynamic test seams, package identity, manifest
  bytes, failure behavior, and structural limits.

## Impact

This change affects only Basins package Python ownership, compatibility forwarding,
focused under-limit contract tests, and OpenSpec. It changes no package schema or
bytes, forcing/calibration policy, registry SQL, CLI/environment contract,
production object-store validation layout, test-corpus layout, CI routing,
database, frontend/display, Slurm, SHUD runtime, or #1903 mapping behavior.
