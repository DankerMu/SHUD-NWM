## Context

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

The current module owns `ProductionSchedulerConfig`, path-mode helpers, and
DB-free preflight helpers. `services.orchestrator.scheduler` lazily imports the
module and re-exports the class; tests also import the owner directly and call
selected private module attributes. The change is structural, but production
configuration and legacy import compatibility make silent drift unacceptable.

## Goals / Non-Goals

**Goals:**

- Keep every resulting Python owner below 1,000 lines and remove the exact guard
  exclusion for the deleted monolithic path.
- Preserve `services.orchestrator.scheduler_config` and scheduler-facade imports,
  class identity, constructor signature, dataclass fields/default factories,
  normalized values, blocker/evidence payloads, and callback seams.
- Preserve the resolve-call allowlist oracle after conversion from module to
  package.

**Non-Goals:**

- No behavioral refactor, dead-code cleanup, caller migration, API change, new
  config option, scheduler/Slurm runtime change, or retention defect fix.
- No split of `tests/test_retention.py` and no removal of its guard exclusion in
  this PR.
- No edits to other large-file exclusions or entropy thresholds.

## Decisions

1. **Use a package with a compatibility barrel.** `config.py` owns the dataclass,
   `path_modes.py` owns path-mode normalization, and `db_free.py` owns DB-free
   preflight classification. `__init__.py` re-exports the class and every existing
   module-level helper. This preserves both public imports and current private test
   seams. Keeping a thin legacy `.py` shim is impossible because a module and
   package cannot share the same import name.
2. **Split on existing dependency closures, not rewrite logic.** Function bodies,
   constants, evaluation order, exception paths, and callbacks through
   `services.orchestrator.scheduler` remain byte-equivalent apart from imports and
   qualified ownership. More files would add cycles without satisfying another
   requirement.
3. **Preserve introspection identity.** The owner package exposes
   `ProductionSchedulerConfig` at the historical module path; the scheduler facade
   continues its existing `__module__` compatibility assignment. A regression
   snapshot compares dataclass init fields, `inspect.signature`, defaults, methods,
   and selected representative outputs to the pre-split contract.
4. **Repair the AST oracle rather than hide code in the barrel.** The resolve-call
   allowlist test scans all `.py` files beneath a package and the single module for
   non-package owners. Leaving the helper in `__init__.py` would mix implementation
   into the compatibility barrel and make later ownership unclear.
5. **One exclusion removed per child PR.** This PR removes only the scheduler
   config path. The retention-test exclusion stays until the second independently
   reviewable #1872 slice passes its own guard and full-suite gates.

## Risk Packs

- Public API / CLI / script entry: **selected** — owner and facade import paths,
  callable signature, and CLI construction must remain stable.
- Config / project setup: **selected** — env defaults, path normalization, and
  scheduler backend validation are the moved behavior.
- File IO / path safety / overwrite: **selected** — symlink, confinement, lexical
  path, missing-root, and resolve allowlist behavior must not drift; no new IO.
- Schema / columns / units / field names: **selected** — dataclass fields and
  evidence/blocker keys remain exact; no schema change.
- Auth / permissions / secrets: **selected** — DB-free credential and permission
  classification moves unchanged and must retain redaction/blockers.
- Concurrency / shared state / ordering: **not selected** — config construction is
  frozen/local and scheduler execution ordering is untouched.
- Resource limits / large input / discovery: **selected** — the 1,000-line guard
  and entropy audit are the change's acceptance boundary.
- Legacy compatibility / examples: **selected** — facade/private helper imports
  and monkeypatch-sensitive callbacks are explicitly retained.
- Error handling / rollback / partial outputs: **selected** — constructor and
  preflight exception/blocker behavior must remain exact; rollback is source
  revert because there is no persisted migration.
- Release / packaging / dependency compatibility: **selected** — module-to-package
  resolution must work under the existing package build without new dependencies.
- Documentation / migration notes: **selected** — compatibility inventory owner
  description changes from a file to a package.
- All NHMS domain packs: **not selected** — no forecast, geometry, DB data,
  Timescale, SHUD, Slurm lifecycle, provider snapshot, manifest, QC, or published
  artifact behavior changes.

## Invariant Matrix

**Governing invariant:** Moving scheduler configuration code SHALL change only
physical ownership: every existing input produces the same object fields,
exception or preflight evidence through the same import/callback surface.

**Source of truth:** `ProductionSchedulerConfig` constructor/dataclass contract,
module attribute set, normalized instance fields, and DB-free preflight payload.

- Producers: `scheduler_config/config.py` plus env default factories.
- Validators/preflight: `path_modes.py`, `db_free.py`, and callbacks through
  `services.orchestrator.scheduler`.
- Storage/cache/query: none; configuration construction is in-memory.
- Public entrypoints: owner import, scheduler facade, CLI plan/config construction.
- Downstream consumers: scheduler core/runtime, file orchestration, candidate
  evidence, and existing tests constructing the facade class.
- Failure paths: blank/relative/unsafe/symlink paths, permission/OS errors,
  credentials, backend/selector rejection, missing required roots.
- Evidence/audit: DB-free runtime evidence/preflight, compatibility inventory,
  package-aware resolve AST guard, entropy audit, line counts.

Regression rows:

- Owner/facade imports plus representative env/direct constructors produce the
  same signature, class identity contract, normalized fields, evidence and
  blockers.
- Blank, symlink, missing, permission, lexical-residue and credential inputs
  produce the same stable rejection or tolerated value.
- Existing scheduler timing, CLI config propagation, runtime-root callbacks and
  direct private-helper tests remain green.

## Boundary-Surface Checklist

- Shared helper root: package barrel preserves all previous top-level names.
- Public entrypoints: owner import and scheduler facade remain unchanged.
- Read/write/delete surfaces: config path inspection only; no writes/deletes added.
- Producer/consumer boundary: env/direct values to frozen config and preflight.
- Stale/idempotency boundary: repeated construction returns equivalent values.
- Unchanged consumers: scheduler execution, retention, DB/display, frontend and
  Slurm behavior.

## Risks / Trade-offs

- **Package conversion can break private attributes or introspection** → enumerate
  and re-export the full prior module surface; compare AST/symbol/signature and run
  direct-import tests.
- **Imports can introduce a cycle with `scheduler.py`** → retain the existing lazy
  scheduler import order and use type-only imports where needed.
- **The resolve guard can become blind** → make it package-aware and prove a
  temporary unallowlisted `.resolve()` mutation fails the guard.
- **Pure moves can conceal semantic edits** → compare function/class ASTs excluding
  ownership/import changes and require full focused plus repository tests.

## Migration Plan

Create the package and compatibility barrel, update the package-aware test oracle,
remove the exact exclusion, run all evidence, then deploy as an ordinary Python
source update. Rollback is reverting the PR; no data or configuration migration is
required.

## Open Questions

None. The second #1872 PR owns retention-test partitioning and final issue closure.
