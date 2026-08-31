## Why

Issue #1903 cannot make its required synthetic Basins fixture adjustment in the
2,772-line production object-store validator because the repository commit hook
rejects any touched non-excluded file above 1,000 lines. Issue #1911 is the second
structural child of #1906 and isolates that physical split from the later mapping
behavior change.

## What Changes

- Split `services/production_closure/object_store_validation.py` into exactly eight
  finite production owners: the historical compatibility/CLI facade plus contracts,
  path-safety, synthetic-fixture, manifest, runtime-staging, consumption, and
  evidence-payload modules.
- Keep the complete historical module surface, CLI behavior, result/evidence bytes,
  dynamic monkeypatch seams, path and cleanup invariants, and downstream imports.
- Require the facade and every new owner to remain below 1,000 lines while keeping
  `.large-file-guard.json` byte-identical.
- Add tracked structure and compatibility oracles whose deterministic literals do
  not read ignored `.workplans` evidence at collection time.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orchestrator-structural-burndown`: require the production object-store validation
  facade split to preserve imports, runtime patch seams, CLI/redaction, evidence and
  artifact identity, path safety, runtime staging, cleanup, and structural limits.

## Impact

This change affects only production-closure Python ownership, facade forwarding,
focused under-limit compatibility tests, and OpenSpec. It changes no synthetic
`.sp.riv`/`.sp.rivseg` bytes, validation schema or blocker ordering, object-store or
registry behavior, database, frontend/display, Slurm scheduling, SHUD execution,
test-corpus layout, selector/CI routing, dependency, or operator command.
