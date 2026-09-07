## Context

Issue #1903 extracts the still-unmerged Basins mapping invariant from PR #1875. The current package path validates canonical required files and safely copies their bytes, but it does not relate `.sp.rivseg.iRiv` to the actual `.sp.riv.Index` set. A malformed mapping can therefore participate in source identity and immutable publication before downstream consumers see it.

Fixture level: **expanded** (upstream suggestion: expanded, agree). Repair intensity: **high** because this is bounded external-file parsing at an immutable publish boundary with zero-partial-output requirements. The minimal mergeable slice is the active OpenSpec fixture, one shared validation path inside the existing six-owner package facade, necessary synthetic-fixture transitions, one focused test owner, selector/meta evidence, and current validation docs. It excludes every other path carried by PR #1875.

## Goals / Non-Goals

**Goals:**

- Make actual unique `.sp.riv.Index` values the sole reach identity authority for package mapping validation.
- Apply one validation result to publication and `basins_package_source_identity` before environment-backed store construction or any output-side effect.
- Bind regular-file checks, byte limits, reads, strict UTF-8 decoding, parsing, identity evidence, and final mapping publication to the same bounded snapshots.
- Preserve standard SHUD `.sp.riv` trailing blocks, non-contiguous indices, single-reach packages, valid package behavior, and all checked-in calibration overrides.
- Return stable `BASINS_RIVSEG_MAPPING_INVALID` or `BASINS_RIVSEG_MAPPING_DEGENERATE` errors with bounded discriminating details.

**Non-Goals:**

- Change discovery eligibility, registry-import geometry/topology, shapefiles, DB crosswalks, runtime package bytes, or already-published immutable packages.
- Require reach indices to be contiguous or 1-based, or require every declared reach to receive a segment.
- Change package/source-identity schema versions, forcing policy, checksum material for valid bytes, or ordinary non-mapping source replacement behavior.
- Remove or modify the six HHE `GEOL_KSATH=2.0` overrides or their exact declaration oracle.
- Carry PR #1875's scheduler evidence compaction, autopipeline probe, frontend changes, direct stable-spec edits, or any node-22 DB/Slurm action.

## Decisions

### D1: Keep the six-owner production facade; add one thin coordinated seam

The parser and bounded snapshot reader live in the existing `basins_package_source_io.py` owner. The historical `basins_package.py` facade imports the leaf validator as a facade-global binding and invokes that binding directly from both public package seams. A patch of the facade validator binding therefore changes both call paths without adding a ninth wrapper to the eight historical forwarding seams. No seventh `basins_package*.py` owner is introduced; every production owner stays below 1,000 lines, `SourceFile` and all frozen public/private facade signatures remain unchanged, and the new snapshot type is an internal frozen subclass.

Alternative rejected: place the parser in the 958-line facade, which immediately breaches the large-file guard. Alternative rejected: add a seventh package owner, which contradicts the archived six-owner structural contract for no architectural need. Alternative rejected: add optional snapshot fields to `SourceFile`, whose constructor signature is a frozen facade contract.

### D2: Validate after canonical source enumeration and before output/store preflight

Both public seams first resolve the inventory, source root, and canonical required `SourceFile` set without an object store. They then call the same mapping validator. Publication constructs an environment-backed `LocalObjectStore`, plans URIs, binds object keys, samples forcing, preflights keys, and acquires the lock only after mapping validation succeeds. This matters because `LocalObjectStore.__post_init__` creates its root.

An explicitly injected store already exists before the call and cannot be uncreated; rejection still creates no new child, lock, object, manifest, or local output under it. Source identity never constructs a store. Discovery remains unchanged so package validation does not silently alter registration/import eligibility.

### D3: One verified descriptor produces each bounded mapping snapshot

For each canonical mapping source, `_open_verified_source_file` binds the trusted source root and rejects symlink, non-regular, and replaced paths. On that same descriptor the validator uses `fstat`, rejects a fixed byte cap, requests at most `limit + 1`, and decodes strict UTF-8. Error details contain only fixed-size cause keys, counts, limits, and a bounded ID list; no raw exception or file content.

The validator returns immutable snapshots containing the source identity, bytes, size, and SHA-256. `_source_identity_from_plan` uses snapshot size/SHA for these two entries, and publication writes these two entries from snapshot bytes before verifying the object. Other source files retain their existing final-open/final-byte behavior.

This snapshot binding is required: the existing publisher intentionally permits an ordinary source file to change after planning and recomputes the manifest from final copied bytes. Reusing that behavior for mapping files would allow valid bytes to be checked and different invalid bytes to be published. A mapping replacement between validation and final write must therefore publish only the validated snapshot, never the replacement.

### D4: Parse declared blocks against actual Index values

The reader skips at most 32 leading blank/comment lines beginning with `#`, `//`, or `%`. The first remaining line declares a positive row count in its first token. After that count line, at most one exact, case-sensitive standard header row is accepted: `.sp.riv` starts with `Index`; `.sp.rivseg` starts with `Index iRiv`. Comments/blanks within the declared data block do not count as rows but remain subject to the byte cap.

For `.sp.riv`, exactly `reach_count` data rows form the reach table; later standard topology/coordinate blocks are ignored. Every reach row supplies a unique bounded integer `Index`; values may be non-contiguous, zero, or negative if SHUD input uses them, but they are not inferred from `1..reach_count`. For `.sp.rivseg`, exactly `segment_count` data rows supply bounded integer `Index` and `iRiv`; an additional non-comment data row is invalid. Segment `Index` uniqueness is intentionally not added by this issue because only reach binding is the governing invariant.

Each integer token is ASCII decimal with an optional leading sign, no underscores, and at most 18 digits before `int()` conversion. Every `iRiv` must belong to the actual reach set. Missing references and all decode/limit/count/header/column/row/duplicate failures map to `BASINS_RIVSEG_MAPPING_INVALID`.

When `reach_count > 1`, `segment_count > 1`, and all segment rows name one reach, validation maps to `BASINS_RIVSEG_MAPPING_DEGENERATE` with reach, segment, and mapped-reach counts plus the bounded mapped ID. A genuine one-reach package remains valid. The gate does not require every reach to be referenced; that stronger completeness rule is not in issue #1903.

### D5: Add a seventh publication-test owner and mechanically route it

New behavior lives in `tests/test_basins_package_publication_rivseg.py`, below 1,000 lines, importing `tests.basins_package_helpers` at module scope. `BASINS_PACKAGE_PUBLICATION_TESTS` becomes the exact sorted seven-owner authority; helper-only selection becomes exactly eight consumers (seven owners plus `tests/test_basins_package.py`) plus the existing selector rider. Tree-derived equality, collectibility, and per-edge constructed RED tests must change together.

The archived six-owner baseline remains frozen at 80 definitions / 88 unique nodes. The new owner is an additive issue #1903 family, not a recapture or rewrite of those cases. The structural spec is modified only to state seven current owners while preserving the original six baseline bodies one-to-one.

### D6: Synthetic fixture edits are controlled transitions, not oracle recapture

Only three existing fixture producers need valid declared blocks:

1. `tests.basins_package_helpers._make_valid_model` emits a genuine one-reach mapping.
2. `tests.basins_registry_import_helpers._make_valid_model` emits `sp_segment_count` unique segment rows distributed deterministically over actual reach IDs.
3. `object_store_validation_fixture.write_synthetic_basins_fixture` emits its declared two reach and two segment rows.

Each transition is compared to the merge-base blob `27dc6aab5a0772c5489b04049eb483a660cf60d8`; only the mapping literals/row construction may change. Existing fixture paths, function signatures, shapefile semantics, and all unrelated bytes remain fixed. The object-store facade's two mapping text hashes are updated with before-blob provenance and every other stable hash unchanged.

The registry partition remains 7 suites / 94 definitions / 96 nodes / 17 integration nodes / helper 19 functions + 1 class + 4 constants. Its tracked oracle may update only `_make_valid_model` source/AST, helper aggregate/self digests, and explicit #1903 transition metadata; an independent before-blob assertion and constructed unrelated-helper mutation make recapture insufficient. All definition, owner, marker, consumer-route, and database-authority rows remain byte-identical.

### D7: Existing real/synthetic compatibility is tested at the right boundary

The repository QHH sample contains source-declared 1,633/3,738 counts but only sampled rows. It is not itself a complete package input. Existing staging normalizes it to complete 5-reach/18-segment blocks before package/import use; the compatibility suite tests those normalized bytes and keeps the committed sample unchanged.

Current validation docs list all seven publication suites. Historical archived evidence and old commands are not rewritten. The issue body's pre-partition commands are adapted to the current owners instead of reintroducing monolith assumptions.

## Risk Packs Considered

- Public API / CLI / script entry: **selected** — publisher and source-identity planner must agree on stable errors and side effects.
- Config / project setup: **selected** — exact seven-entry calibration declaration and `.large-file-guard.json` must remain unchanged; no config is introduced.
- File IO / path safety / overwrite: **selected** — verified descriptor, same-snapshot publication, fixed cap, and zero output on refusal.
- Schema / columns / units / field names: **selected** — declared counts and `Index` / `iRiv` columns are the contract.
- Auth / permissions / secrets: **not selected** — no credential or authorization surface; existing unreadable/path errors remain owned by source validation.
- Concurrency / shared state / ordering: **selected** — validation must precede environment-store construction and snapshots must prevent validation-to-copy replacement drift.
- Resource limits / large input / discovery: **selected** — fixed byte/skip/integer/report limits; no new directory discovery.
- Legacy compatibility / examples: **selected** — single reach, non-contiguous IDs, trailing blocks, existing package and downstream fixtures.
- Error handling / rollback / partial outputs: **selected** — failures precede every new store/output effect; injected-store children remain empty.
- Release / packaging / dependency compatibility: **not selected** — no dependency or distribution change.
- Documentation / migration notes: **selected** — current validation commands and active contract change; no data migration.
- Geospatial / CRS / basin geometry: **selected only at identity boundary** — `iRiv` selects a reach, but geometry/CRS/shapefile bytes are untouched.
- Hydro-met time series / forcing windows: **not selected** — forcing behavior remains unchanged.
- SHUD numerical runtime / conservation / NaN: **selected** — a false reach mapping corrupts river routing; valid runtime bytes remain untouched.
- PostGIS / TimescaleDB domain behavior: **not selected** — no DB read/write or schema change.
- Slurm production lifecycle / mock-vs-real parity: **not selected** — no scheduler/job action; node-22 evidence is DB-free and disposable.
- External hydro-met providers / snapshot reproducibility: **not selected** — no provider data.
- Run manifest / QC provenance: **not selected** — no run/QC payload change.
- Published NHMS artifacts / display identity: **selected** — invalid mapping bytes must not enter immutable package identity or storage.

## Boundary-Surface Checklist

- Shared helper roots: `basins_package_source_io`, facade wrappers, `_package_source_files`, `_source_identity_from_plan`, and object-store write helpers.
- Public entrypoints: `publish_basins_package`, `basins_package_source_identity`, and existing CLI/dry-run callers.
- Read surfaces: exactly one canonical `.sp.riv` and `.sp.rivseg` beneath the verified model source root.
- Write/delete/overwrite surfaces: environment store root, package children, lock/temp/object/manifest, local output; all absent on mapping refusal.
- Staging/publish/rollback surfaces: snapshot-bound mapping write; existing cleanup for later failures remains unchanged.
- Producer/consumer evidence boundaries: source mapping bytes to source identity/package manifest; synthetic fixture writers to package/registry/QHH/object-store suites.
- Stale-state/idempotency boundaries: validation-to-plan and plan-to-write source replacement; repeated unchanged valid inputs retain identity.
- Unchanged downstream consumers: discovery, forcing, calibration, registry import, QHH bootstrap, runtime staging, display, scheduler evidence.

## Invariant Matrix

- Governing invariant: source identity and publication may use `.sp.riv` / `.sp.rivseg` bytes only when every segment binds an actual unique reach and a multi-reach/multi-segment mapping has not collapsed to one reach; the bytes used are the bytes validated.
- Source-of-truth identity/contract: bounded snapshots of canonical `.sp.riv` reach block and `.sp.rivseg` segment block, opened beneath the verified source root.
- Producers: external Basins sources (unchanged) and three synthetic fixture writers (controlled transition only).
- Validators/preflight: canonical required-file selection, verified descriptor open, snapshot byte/digest evidence, parser, membership and degeneracy checks.
- Storage/cache/query: source identity and immutable local object store only after validation; no DB/cache mutation.
- Public routes/entrypoints: package publication and source identity; CLI/dry-run callers inherit them.
- Frontend/downstream consumers: registry import, QHH bootstrap, SHUD runtime staging, output parsing, and display consume unchanged accepted bytes.
- Failure paths/rollback/stale state: malformed, replaced, oversized, invalid UTF-8, count/header/column/duplicate/reference/degenerate states return bounded domain errors before output; no rollback is needed for the mapping gate.
- Evidence/audit/readiness: focused/full local tests, selector/oracle transitions, frozen-SHA node-27 backend receipt, DB-free disposable node-22 HHE receipt, unchanged calibration exact-set oracle.
- Regression rows:
  - Multiple actual reaches and segments covering at least two reaches -> source identity and publish succeed.
  - Reach indices `{10, 30}` and segment references `{10, 30}` -> succeed without a `1..N` assumption.
  - Bounded leading comments, exact optional headers, blank/comment lines within blocks, and trailing `.sp.riv` blocks -> parse declared data only and succeed.
  - Missing `iRiv`, duplicate reach Index, overlong integer, invalid UTF-8, invalid/non-positive count, bad header/column, truncated/extra segment rows, or over-limit/growing file -> `BASINS_RIVSEG_MAPPING_INVALID`, bounded details, zero output/store mutation.
  - Multiple reaches/segments all naming one reach -> `BASINS_RIVSEG_MAPPING_DEGENERATE` with exact bounded counts/ID and zero output/store mutation.
  - Valid mapping replaced after validation -> identity/publication retain the validated snapshot or reject; invalid replacement bytes are never published.
  - One-reach package, forcing/calibration package, normalized QHH fixture, registry import, and object-store validation -> existing behavior remains compatible.
  - Any old PR #1875 scheduler, autopipeline, frontend, calibration, stable-spec, DB/runtime/Slurm path -> absent from the diff.

## Risks / Trade-offs

- [A legitimate SHUD variant uses another header shape] -> accept only documented optional headers and bounded comments; validate disposable real HHE files before merge rather than broad guessing.
- [A valid network exceeds the cap] -> choose a fixed cap above measured real files and report its value in the receipt; reject stably instead of allocating without bound.
- [Snapshot memory doubles two files] -> both are individually capped and only the two mapping files are retained; this is the smallest way to bind validation to publication without changing every `SourceFile` contract.
- [A multi-reach model intentionally maps all current segments to one reach] -> fail closed under issue #1903; any exception needs its own explicit contract.
- [Synthetic fixture edits weaken frozen partition evidence] -> before-blob allowlist plus constructed RED prevents arbitrary oracle recapture.

## Migration Plan

1. Land validator, controlled fixture transitions, selector/oracle updates, and tests; no DB/object migration.
2. Validate frozen code on node-27 and real HHE mapping files copied to disposable node-22 roots.
3. New identity/publication attempts fail closed for invalid mappings; existing immutable objects remain untouched.
4. Rollback is code-only: revert the feature commit. Calibration declarations and production Basins bytes remain unchanged.

## Open Questions

None.
