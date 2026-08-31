## Context

Issue #1911 is #1906 child 2 and depends on the completed #1910 split. At baseline
`3968123a424ff05ec07be11ef5d52a185e73da37`,
`services/production_closure/object_store_validation.py` is 2,772 lines and is not
excluded from the 1,000-line guard. Its module contract contains 159 non-dunder names,
123 callable signatures and eight dataclasses. Production `slurm_validation.py` and
the primary validation test import the historical module; tests patch eight facade
callables during real CLI, evidence-write, fixture, package, registry and staging
paths. Static re-export can therefore preserve attribute presence while moved leaves
silently bypass the patched binding.

Fixture level: expanded (agrees with issue). Repair intensity: high because this
shared CLI owns evidence writes, descriptor/path safety, bounded manifest and runtime
staging reads, registry/object-store consumption, cleanup, redaction and compatibility
seams. The accountability token is `high`.

## Goals / Non-Goals

**Goals:**

- Produce exactly eight sub-1,000-line production files with a stable historical
  facade and acyclic responsibility owners.
- Preserve every baseline facade name, callable signature, dataclass shape, public
  entrypoint owner, class identity, runtime patch seam, importer and CLI mode.
- Preserve summary/status/blocker ordering, redacted evidence and stdout, manifest
  and checksum material, runtime-staging receipts and budgets, path containment,
  no-follow behavior, cleanup ownership and synthetic fixture bytes.
- Keep the guard byte-identical and add no route, dependency or userspace migration.

**Non-Goals:**

- No #1903 validator, mapping error, test, or synthetic `.sp.riv`/`.sp.rivseg` byte
  change; the 27-file synthetic fixture tree remains byte-identical.
- No Basins package or test-corpus split, helper cleanup, behavior rewrite, caller
  migration, schema/status/blocker/redaction change, database/frontend/display,
  Slurm scheduling or SHUD runtime change.
- No `.large-file-guard.json`, selector, CI workflow or active docs change. Existing
  `services/production_closure/**` routing already owns every sibling owner; selector
  tests must pass unchanged or an actual failure is diagnosed as a deviation.

## Decisions

### D1: Use one facade plus seven finite responsibility owners

The final production layout is exactly:

- `object_store_validation.py`: `EvidenceWriter`, `ProductionObjectStoreConfig`,
  `validate_object_store`, CLI dispatch/`main`, compatibility exports and the thin
  coordinators needed to resolve historical facade bindings at call time;
- `object_store_validation_contracts.py`: error, runtime/manifest dataclasses,
  immutable defaults, regexes, limits and small hash/JSON primitives;
- `object_store_validation_path_safety.py`: prefix/configured-path validation,
  no-follow component traversal, fd identity helpers and lane/store containment;
- `object_store_validation_fixture.py`: the synthetic Basins fixture and shapefile
  writers, moved byte-for-byte apart from qualified owner calls;
- `object_store_validation_manifest.py`: raw worker-output handling, migration and
  package/stored-manifest verification/checksum reconstruction;
- `object_store_validation_runtime.py`: bounded package/prefix collection, runtime
  workspace preparation, staged writes/reads and receipt construction;
- `object_store_validation_consumption.py`: registry/API/runtime consumption and
  validation-owned scratch cleanup/rollback evidence;
- `object_store_validation_evidence.py`: preflight/environment/summary/blocker and
  deterministic acceptance payload builders.

The dependency DAG is contracts -> path safety -> fixture/manifest -> runtime ->
consumption -> facade, with evidence payloads depending only on contracts. No leaf
imports the historical facade at import time. Baseline closure estimates leave each
owner below 800 lines before wiring; if imports push an owner near the threshold, the
implementation moves the smallest complete closure between these named owners rather
than adding a ninth file or changing responsibilities without fixture revision.

Alternative rejected: converting the module to a package changes the `python -m`
path and private owner metadata; arbitrary line slices hide fd/cleanup ownership;
more micro-modules increase cycles and forwarding without another requirement.

### D2: Limit runtime forwarding to the eight observed facade callables

The finite patch authority is:

- `_verify_stored_objects`, `import_basins_registry`, `write_inventory`,
  `write_basins_migration_report`, `publish_basins_package`,
  `atomic_write_bytes_no_follow`, `ensure_directory_no_follow`, and
  `validate_object_store` as consumed by CLI dispatch.

`validate_object_store`, CLI dispatch, `EvidenceWriter`, config and any thin
coordinator that directly consumes these names stay physically in the facade or pass
the current facade-bound callable explicitly into a leaf. The manifest, fixture,
runtime and consumption leaf functions accept only the dynamic dependencies their
call path consumes; no function-local or import-time facade back-import is allowed.
A parameterized test must invoke each real high-level path and prove the patch bites,
not merely assert the facade attribute exists.

Other names use plain re-export. `os`, `LocalObjectStore`, `ObjectStoreError`,
`SafeFilesystemError` and safe-fs internals retain shared imported module/class
identity, so attribute patches on their authority modules remain visible. Constants
are copied verbatim to contracts and re-exported; no speculative dependency injection
is added for values that tests do not patch.

Alternative rejected: plain `from leaf import helper` for all names breaks downstream
leaf lookup; leaf-to-facade back-import creates partial-initialization cycles; passing
every stdlib/class/constant through every helper is noise rather than compatibility.

### D3: Preserve the complete facade/dataclass/import/CLI contract

The tracked test embeds deterministic literals derived from the baseline: all 159
non-dunder names, 123 callable signatures, eight dataclass field contracts, two live
importers, public entrypoint `__module__` values and stable class identities. Post-split
facade names must be a superset; every baseline signature/dataclass shape remains
exact. Private moved bodies may change `__module__`; `validate_object_store`,
`write_synthetic_basins_fixture` and `main` remain exposed through the historical
module, and the public coordinator/CLI ownership stays there.

Fresh processes import both consumers and execute
`python -m services.production_closure.object_store_validation` usage failure:
return code 2, empty stdout, usage/no-option text, and no traceback. Both click and
argparse paths retain exit 0/1/2 semantics and redact stdout/errors before output.

### D4: Compare behavior through existing high-level oracles and same-platform receipts

Baseline local suite results are recorded per file: object-store validation 110,
Slurm validation 70, readiness validation 349 passed/2 skipped, Basins publication
87/1, registry import 78/18, reingest 2/4, scheduler registry 59, and selector 405.
The synthetic fixture has 27 regular files with tree digest
`8601d5d0b7e8317bb1f111894bfdc46da0f22e9c9c57cdf0bf0cc63bb7629a88`.
After movement, tracked oracles compare the facade contract and fixture bytes, while
existing suites cover ready/blocked/error payloads, manifest checksums, path races,
resource limits, registry import, staging and cleanup. No assertion, expected bytes,
skip, marker or fixture is weakened.

Node-27 first captures the focused suite set at baseline SHA and then at the frozen
implementation SHA in detached worktrees using the active project environment without
changing the active checkout. Linux results are compared pre/post on node-27, not to
macOS skip totals. Node-22 is not used because no Slurm scheduling or SHUD runtime
behavior changes.

## Risk Packs Considered

- Public API / CLI / script entry: selected - module/CLI consumers, signatures, exit
  modes and redaction must remain exact.
- Config / project setup: selected - env-derived config and guard digest/threshold are
  contracts; no new configuration.
- File IO / path safety / overwrite: selected - evidence, fixture, raw-lane and runtime
  writes plus no-follow/fd containment move unchanged.
- Schema / columns / units / field names: selected - evidence/result schemas, blocker
  order, manifest/checksum and receipt fields remain byte/shape equivalent.
- Auth / permissions / secrets: selected for redaction/credential safety only - no auth
  policy changes; stdout/evidence must not expose credentials or signed query data.
- Concurrency / shared state / ordering: selected - TOCTOU, fd identity, staged object
  verification and cleanup order remain exact.
- Resource limits / large input / discovery: selected - bounded manifest/raw/runtime
  reads, node/file/depth/byte budgets and scoped discovery remain exact.
- Legacy compatibility / examples: selected - complete facade, private test seams,
  production importer, standalone and packaged CLI remain compatible.
- Error handling / rollback / partial outputs: selected - typed error codes, blocked
  evidence lanes, raw cleanup and validation-owned deletion remain exact.
- Release / packaging / dependency compatibility: selected - eight sibling modules
  import in fresh processes without a new dependency or cycle.
- Documentation / migration notes: not selected - no user/operator migration; active
  commands and selector/docs paths stay unchanged.
- Geospatial / CRS / basin geometry: selected for compatibility only - shapefile and
  crosswalk bytes move unchanged; no geometry semantics change.
- Hydro-met time series / forcing windows: selected for compatibility only - forcing
  fixture, manifest material and staging receipts remain exact.
- SHUD numerical runtime / conservation / NaN: not selected - no solver execution or
  numerical policy changes.
- PostGIS / TimescaleDB domain behavior: not selected - registry live-import seam is
  patched/focused only; no database schema/query/data change.
- Slurm production lifecycle / mock-vs-real parity: not selected - packaged CLI import
  compatibility is tested, but no submit/poll/cancel/scheduler behavior changes.
- External hydro-met providers / snapshot reproducibility: not selected - no provider.
- Run manifest / QC provenance: not selected - this lane owns object-store validation
  evidence, governed under artifact identity below rather than run/QC manifests.
- Published NHMS artifacts / display identity: selected - package/object/manifest URI,
  checksum and runtime receipt identity remain exact; no display route changes.

## Boundary-Surface Checklist

- Shared roots: contracts/path helpers are leaf-most; no import-time facade back-edge.
- Public entrypoints: historical `validate_object_store`, standalone `main` and packaged
  `slurm_validation` consumer remain intact.
- Read/write/delete: bounded manifest/object reads, atomic evidence/raw/fixture/runtime
  writes and owned scratch deletion remain intact.
- Staging/publish/rollback: package publication, verified-object handoff, runtime staging,
  quarantine and cleanup order remain intact.
- Producer/consumer: Basins fixture/package -> stored verification -> registry/API/runtime
  consumption -> redacted summary/evidence.
- Stale/idempotency: existing lane/object/scratch, tamper-after-verify and workspace
  collision behavior remain fail-closed.
- Unchanged consumers: `slurm_validation`, readiness summaries, Basins publication/
  registry/reingest, scheduler registry and selector routing.

## Invariant Matrix

- Governing invariant: moving an object-store validation helper to a sibling owner
  SHALL not change any import, value, byte, side effect, failure, cleanup action or
  runtime-patched dependency observed through the historical facade or its CLI.
- Source-of-truth identity/contract: 159 facade names, 123 signatures, eight dataclass
  shapes, eight dynamic seams, two importers, 27-file fixture digest, evidence/result
  schemas, blocker order, guard digest and owner line counts.
- Producers: exactly eight production files; synthetic fixture/package/registry/runtime
  producers move only and retain bytes/arguments.
- Validators/preflight: config/prefix/path/manifest/stored-object/staging validators plus
  facade contract and forwarding tests.
- Storage/cache/query: local object store and optional registry-import seam unchanged;
  no cache or DB implementation change.
- Public routes/entrypoints: historical module `validate_object_store` and `main`,
  `python -m`, and packaged `nhms-production validate-object-store` consumer.
- Downstream consumers: readiness summaries, `slurm_validation`, package publication,
  registry import/reingest, scheduler registry and existing tests.
- Failure/rollback/stale state: stable typed errors/blockers, redaction, zero external
  writes, raw cleanup, validation-owned scratch deletion and existing-object refusal.
- Evidence/audit: baseline/post comparator, fixture digest, CLI/import smokes, focused/
  full pytest, ruff, entropy/guard, strict OpenSpec and node-27 pre/post receipt.
- Regression rows:
  - valid deterministic config -> identical fixture tree, package/manifest/checksum,
    staged receipts, evidence files and ready summary through the historical CLI;
  - each facade dependency patched -> the actual writer/package/verify/registry/staging
    or CLI path observes it and the existing biting assertion changes/fails as expected;
  - symlink/ancestor swap/oversize/stale/tampered/pre-existing input -> identical stable
    refusal/blocker with no external or unowned cleanup side effect;
  - unchanged `slurm_validation`/readiness/Basins/scheduler consumer -> imports and
    focused behavior remain green without routing changes.

## Risks / Trade-offs

- [Static re-export bypasses monkeypatch] -> explicit facade injection limited to the
  eight observed callables plus biting high-level tests.
- [Owner split introduces cycle] -> contracts/path DAG, no import-time facade back-edge,
  fresh-process imports and standalone `python -m` smoke.
- [Path/evidence semantics drift during movement] -> preserve complete fd/path/runtime
  closures, exact fixture/evidence snapshots and existing adversarial corpus.
- [One owner exceeds threshold] -> move a complete closure only among the eight frozen
  owners; do not add an owner or exclusion without fixture revision.
- [Pure move hides cleanup or #1903 bytes] -> body/fixture byte differences are recorded
  deviations and require exact equivalence evidence; #1903 bytes are a hard non-goal.

## Migration Plan

Capture baseline contracts, move complete responsibility closures, wire finite facade
forwarding, run exact comparisons and full evidence, then merge as a source-layout
update. Rollback is reverting the PR; no object, database, configuration or environment
migration occurs. #1912 remains blocked until this child and its post-merge closure are
complete.

## Open Questions

None.
