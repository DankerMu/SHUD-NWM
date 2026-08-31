## Context

Issue #1910 is #1906 child 1 and has no dependency. At baseline
`44fcd41613e806bc309fbdb41816aa3406cbaecd`,
`workers/model_registry/basins_package.py` is 3,120 lines and is not excluded from
the 1,000-line guard. The existing focused oracle is 340 passed and 23 skipped.
Ten files import the module: six production/script callers (`cli.py`, reingest,
QHH bootstrap, registry import, production object-store validation, scheduler file
registry) plus four test modules. Two tests bind the historical module object and
patch helpers during live publish/path-race calls. A static re-export can therefore
appear compatible while the moved leaf still calls its unpatched local binding.

Fixture level: expanded (agrees with issue). Repair intensity: high because the
module owns verified source reads, immutable publish/checksum material, atomic
object writes, locks, cleanup, and compatibility seams.

## Goals / Non-Goals

**Goals:**

- Produce exactly six sub-1,000-line production files with a stable historical
  facade and acyclic responsibility owners.
- Preserve all pre-split facade attributes, public signatures/types/constants, and
  every observed runtime monkeypatch seam.
- Preserve valid source/package identity, manifest bytes/checksums, object effects,
  forcing/calibration policy, idempotency, typed failures, and cleanup.
- Keep the guard configuration byte-identical and add no route, dependency, or
  userspace migration.

**Non-Goals:**

- No #1903 rivseg validator, mapping error, test, or fixture-byte change.
- No split of object-store validation or either test monolith.
- No refactor/cleanup, caller migration, schema/version change, forcing/calibration
  change, registry SQL, CLI/env, database, frontend/display, Slurm, or SHUD runtime.
- No `.large-file-guard.json`, selector, CI workflow, or active docs change. The
  existing `workers/model_registry/**` directory rule already selects the governed
  Basins suites for sibling owner paths; `tests/test_select_ci_tests.py` must pass
  unchanged after all five new files exist, or the failure becomes a diagnosed
  deviation rather than permission to edit routing speculatively.

## Decisions

### D1: Use six finite sibling modules

The final production layout is exactly:

- `workers/model_registry/basins_package.py`: three public entrypoints,
  compatibility re-exports/wrappers, source-plan coordination, schema/source
  identity constants;
- `workers/model_registry/basins_package_contracts.py`: `BasinsPackageError`,
  `SourceFile`, `ObjectStoreParent`, hashing/JSON primitives and small shared types;
- `workers/model_registry/basins_package_inventory.py`: inventory/model/root
  resolution, canonical required-file planning, source plan/identity helpers;
- `workers/model_registry/basins_package_source_io.py`: verified descriptor opens,
  source path containment/symlink checks, walking/evidence/time sampling;
- `workers/model_registry/basins_package_manifest.py`: forcing metadata/checksum,
  manifest assembly/read/consistency/calibration/success material;
- `workers/model_registry/basins_package_object_store.py`: key/path/no-follow parent
  operations, locks, streaming/atomic write/read/verify/preflight.

Baseline closure arithmetic before imports/wiring is: facade entrypoints 389 lines,
inventory/planning 604, forcing/manifest 658, object-store core plus verification
678, and source IO/path/evidence about 580; contracts/hash material is under 100.
Only eight callable wrappers are required (D2), while other historical private
names are plain re-exports (D3), so each owner has material headroom below 1,000.
If imports push an owner near the limit, implementation moves the smallest complete
closure to an adjacent named owner; it does not add a seventh module or change
responsibility names without fixture revision.

Alternative rejected: converting `basins_package` to a package changes a heavily
used module path and complicates `__module__`; nine micro-modules add cycles and
compatibility glue without another requirement; arbitrary line ranges hide
ownership.

### D2: Preserve dynamic patch seams with facade wrappers and explicit injection

The finite runtime-patch authority is:

- `_package_source_files`, `_walk_source_files`, `_csv_time_evidence`,
  `_migration_source_file_evidence`, `_source_file_size`,
  `_write_file_to_store_streaming`, `_verify_object_bytes`, and
  `_object_size_and_checksum_streaming`;
- facade-bound `LocalObjectStore`, `ObjectStoreError`, `os`,
  `MAX_EXISTING_MANIFEST_BYTES`, and `FORCING_SAMPLE_FILE_LIMIT` attribute access.

Wrapper/injection is required only for those eight callable seams. Their direct
callers are the three public entrypoint/coordinator paths plus
`_forcing_metadata`, `_verify_existing_manifest_consistency`,
`_write_source_file_to_store`, `_verify_object_bytes`, and `_directory_evidence`;
these callers accept or forward the current facade-bound callable at runtime. Leaf
modules never import the facade at module import time. Existing tests that patch a
facade helper must still alter the real publish/migration/path-race call; a
parameterized compatibility test inventories all eight callable seams and fails if
a forwarding edge is removed.

Object/constant seams use a different, explicit mechanism: facade and leaves bind
the same imported `os` module object and `LocalObjectStore`/`ObjectStoreError` class
objects, so attribute-level monkeypatches remain shared; limits used by moved code
are passed as values from facade coordination only when tests patch the facade
binding. One biting high-level oracle is required for each governed `os`, class,
error, and limit seam. They are not expanded into per-helper dependency injection.

Alternative rejected: plain `from leaf import helper` re-export preserves lookup
but not downstream leaf calls for the eight patchable callables; function-local
back-import works but creates opaque cycles and makes ownership depend on partial
initialization. Injecting `os` or a shared class into every low-level helper is
noise because attribute monkeypatching already mutates the shared object.

### D3: Preserve the complete historical module attribute set without freezing new leaf internals

The saved baseline contains every non-dunder module name plus signatures and owner
metadata. Post-split facade names must be a superset of the baseline, and every
baseline callable keeps its signature. The eight patchable callable seams use
wrappers; the other roughly 80 private helpers are plain re-exports, not wrappers.
`BasinsPackageError`, `SourceFile`, schema constants, `LocalObjectStore`, and
`ObjectStoreError` retain object identity through all existing import paths.
Private implementation `__module__` may change only for non-entrypoint bodies moved
to leaves; public entrypoints remain owned by the historical facade.

### D4: Compare semantics through existing high-level entrypoints

Structure is not proved by import success alone. Before movement, persist module
contract/digests and all ten importers by name, plus representative valid publish/
source-identity and stable error payloads. Capture local results per suite rather
than one portable skip total: package 4 passed; publication 87 passed/1 skipped;
registry import 78 passed/18 skipped; reingest 2 passed/4 skipped; production
object-store validation 110 passed; scheduler file registry 59 passed. After
movement, compare exact valid identities, manifest/package bytes and success
payload; run all existing conflict, symlink, TOCTOU, lock, streaming, idempotency,
forcing, migration, registry/reingest and production-consumer tests. Node-27 first
captures the same pre-split suites at baseline SHA, then compares post-split frozen
SHA on that platform; Linux skip differences are not compared to macOS totals.
Existing tests are the behavioral oracle; no assertion, platform-specific skip,
fixture, or expected bytes may be weakened.

## Risk Packs Considered

- Public API / CLI / script entry: selected - public functions/types/importers and
  CLI consumers must retain signatures and errors.
- Config / project setup: selected - guard digest/line threshold are gates; no new
  config.
- File IO / path safety / overwrite: selected - verified descriptors, no-follow,
  containment, streaming/atomic write and cleanup move unchanged.
- Schema / columns / units / field names: selected - package schema, manifest keys,
  checksums and source identity remain byte/shape equivalent.
- Auth / permissions / secrets: not selected - no auth/secret surface; permission
  and unreadable-path errors are covered under file IO.
- Concurrency / shared state / ordering: selected - publish lock, existing manifest,
  CAS-like verification and cleanup ordering remain.
- Resource limits / large input / discovery: selected - bounded manifest/forcing
  sampling/walking plus sub-1,000-line structure remain.
- Legacy compatibility / examples: selected - complete facade attributes, private
  test seams, six importers, valid fixtures, reingest and scheduler consumers.
- Error handling / rollback / partial outputs: selected - typed errors and no local/
  object partial output on refusal remain.
- Release / packaging / dependency compatibility: selected - six sibling modules
  must import in fresh processes without new dependencies/cycles.
- Documentation / migration notes: not selected - no operator migration; active
  docs/selector paths stay unchanged.
- Geospatial / CRS / basin geometry: not selected - geometry bytes are copied but
  not semantically changed; registry geometry behavior unchanged.
- Hydro-met time series / forcing windows: selected only for compatibility - forcing
  inclusion/exclusion and bounded time evidence remain exact.
- SHUD numerical runtime / conservation / NaN: not selected - no solver/runtime
  change.
- PostGIS / TimescaleDB domain behavior: not selected - no DB code/schema/data.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler or
  Slurm action.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider source.
- Run manifest / QC provenance: not selected - Basins package manifest is governed
  under published-artifact identity, not run/QC provenance.
- Published NHMS artifacts / display identity: selected - immutable package bytes,
  manifest/checksum/source identity stay exact.

## Boundary-Surface Checklist

- Shared roots: contracts is leaf; source IO/object-store do not import facade.
- Public entrypoints: publish, source identity, migration report through old module.
- Read/write/delete: verified source reads and atomic object/output writes unchanged.
- Publish/rollback: lock, preflight, manifest reservation/verification and cleanup.
- Producer/consumer: source bytes -> source identity/package manifest -> registry,
  reingest, production validation and scheduler consumers.
- Stale/idempotency: existing immutable manifest/object and lock behavior unchanged.
- Unchanged consumers: CLI, QHH bootstrap, registry import, reingest, production
  object-store validation, scheduler registry.

## Invariant Matrix

- Governing invariant: moving a Basins package helper to a sibling owner SHALL not
  change any value, side effect, failure, or runtime-patched dependency observed
  through the historical facade.
- Source-of-truth identity/contract: baseline facade names/signatures/object
  identities, dynamic seam table, source/package identities, manifest/package bytes,
  success/error payloads, guard digest and line counts.
- Producers: six finite owner files; no data producer change.
- Validators/preflight: existing package/inventory/path/object checks plus facade
  contract and seam-forwarding tests.
- Storage/cache/query: immutable local object store only, unchanged; no DB/cache.
- Public routes/entrypoints: historical module and three functions; CLI callers.
- Downstream consumers: reingest, registry import, QHH bootstrap, production closure,
  scheduler file registry and existing tests.
- Failure/rollback/stale state: typed error, zero partial outputs, lock/object cleanup,
  existing manifest conflict/idempotency.
- Evidence/audit: baseline/post comparator, focused/full pytest, fresh import, ruff,
  entropy/guard, strict OpenSpec, node-27 frozen SHA.
- Regression rows:
  - valid fixture -> identical source identity, package bytes/manifest/checksum and
    success payload through old entrypoints;
  - facade helper patched during publish/path race -> actual leaf call observes the
    patch and existing failure assertion bites;
  - invalid/symlink/conflict/lock fixture -> identical error details and no partial
    outputs;
  - unchanged CLI/reingest/registry/production/scheduler caller -> import and focused
    behavior remain green.

## Risks / Trade-offs

- [Static re-export bypasses monkeypatch] -> explicit facade injection plus eight-
  seam parameterized oracle.
- [Owner split introduces cycle] -> contracts leaf, no import-time facade back-edge,
  fresh-process importer smoke.
- [Manifest/identity drifts from import rewiring] -> exact byte/identity snapshots
  and full existing focused corpus.
- [One owner remains over threshold] -> move a complete closure between the six
  frozen owners; line count is checked before implementation completion.
- [Pure move hides cleanup] -> no cleanup; AST/body changes are deviations requiring
  a specific equivalence test.

## Migration Plan

Capture baseline contracts, move responsibility closures, wire facade injection,
run exact comparisons and full evidence, then merge as a source-layout update.
Rollback is reverting the PR; no object, DB, config, or environment migration.
#1911 remains blocked until this child is merged and post-merge closure completes.

## Open Questions

None.
