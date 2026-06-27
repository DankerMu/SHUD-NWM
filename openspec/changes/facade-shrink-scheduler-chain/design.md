## Context

`scheduler.py` and `chain.py` remain large because they are compatibility
facades as well as runtime entrypoints. Direct deletion of old private names
would break tests and downstream monkeypatch paths. The shrink strategy is
therefore to move implementation bodies into owner modules and keep small
compatibility aliases/wrappers in the facade until a later caller-migration
slice removes them.

## Goals

- Reduce facade line count with behavior-preserving owner-module moves.
- Keep legacy private names importable from the facade.
- Keep monkeypatch-sensitive paths wired through facade globals where tests
  currently patch the old module.
- Update inventories in the same change as every new compatibility alias.

## Non-Goals

- No Slurm behavior change, scheduler pass behavior change, DB/schema change,
  API/frontend behavior change, production topology change, or station-MVT
  claim.
- No entropy hard-gate enablement and no `.entropy-baseline/latest.json` write.
- No compatibility removal without explicit caller migration and focused proof.

## Invariants

- Scheduler preflight must return the same blockers/check shape for database,
  storage-root, template, environment, SHUD executable, gateway, and GRIB env
  checks.
- Existing tests may continue to import or monkeypatch private names from
  `services.orchestrator.scheduler`.
- Entropy compatibility-facade guard must report zero scheduler/chain signals
  after inventory updates.
