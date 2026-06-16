## Context

Issue #493 is part of #492. `workers/forcing_producer` writes forcing packages and records `met.forcing_version.forcing_package_uri`, `checksum`, and `lineage_json`; `lineage_json` carries `forcing_package_manifest_uri`, `forcing_package_manifest_checksum`, and `output_files`. `TilePublisher._publish_qdown_from_database()` currently publishes display artifacts and mirrors `runs/<run_id>` only. The shared `/ghdc/data/nwm/object-store` therefore misses `forcing/...` for newly published q_down runs.

Risk triage:

- Issue type: bugfix
- Project profile: NHMS
- Blast radius: high
- Fixture level: expanded
- Repair intensity: high
- Why: publish/copyback boundary, path and symlink safety, DB metadata contract, checksum/audit lineage, two-node production artifact identity.

## Goals / Non-Goals

**Goals:**

- Mirror the exact forcing package referenced by each successfully published q_down run to the shared object-store.
- Keep forcing packages in object-store keyspace, never under `NHMS_PUBLISHED_ARTIFACT_ROOT`.
- Fail publication loudly when forcing metadata or source package integrity is missing or unsafe.
- Preserve existing `runs/<run_id>` copyback behavior and exact-root validation.

**Non-Goals:**

- No historical backfill; #494 owns dry-run/apply for already published runs.
- No change to forcing producer package layout.
- No change to q_down display artifact schema beyond copyback lineage details.
- No production file operation outside test/local verification.

## Decisions

- Use `LEFT JOIN met.forcing_version` in q_down discovery. This preserves q_down rows and lets copyback validation report missing forcing metadata with run-level details. Discovery reads `forcing_package_uri`, `checksum`, and raw `lineage_json`; manifest URI/checksum are parsed from lineage in Python and must tolerate dict, JSON string, null, and malformed values with stable failure details.
- Normalize forcing package references through `LocalObjectStore.normalize_key()`, then allow only `forcing/<source>/<cycle>/<basin_version_id>/<model_id>` with safe non-empty segments. This avoids reusing the `runs/<run_id>` tree parser for a different keyspace.
- Validate `forcing_package.json` before copying: the source key must resolve to a real object-store directory, not a regular file or symlink; the manifest must be present; its SHA-256 must match `met.forcing_version.checksum`; and any lineage `forcing_package_manifest_checksum` must match the DB checksum.
- Copy forcing packages after q_down layer selection and before display artifact writes/DB publish commit. A forcing copyback failure must leave the stable cycle manifest and published files unchanged.
- Report lineage as `object_store_copyback.runs` plus `object_store_copyback.forcing_packages`, with package-level dedupe by normalized key.

## Risk Packs Considered

- Public API / CLI / script entry: not selected - no public route/CLI contract changes.
- Config / project setup: selected - behavior is gated by existing `NHMS_OBJECT_STORE_COPYBACK_ROOT`.
- File IO / path safety / overwrite: selected - shared object-store copyback reads/writes directories and must reject traversal/symlink/non-directory sources.
- Schema / columns / units / field names: selected - q_down discovery reads `met.forcing_version` metadata and lineage fields.
- Auth / permissions / secrets: not selected - no credential handling changes.
- Concurrency / shared state / ordering: selected - publication must copy before stable display artifacts are advanced.
- Resource limits / large input / discovery: selected - package tree copy must stay scoped to one normalized prefix and preserve existing bounded tree checks.
- Legacy compatibility / examples: selected - existing run copyback and exact-root skip behavior must remain compatible.
- Error handling / rollback / partial outputs: selected - forcing copyback failures must fail publish without advancing display artifacts.
- Release / packaging / dependency compatibility: not selected - no dependencies or packaging changes.
- Documentation / migration notes: not selected - #497 owns production runbook/env docs.
- PostGIS / TimescaleDB domain behavior: not selected - no schema migration or Timescale-specific query.
- Hydro-met time series / forcing windows: selected - forcing package identity includes source/cycle/basin/model, but content semantics are not changed.
- Run manifest / QC provenance: selected - manifest checksum is the producer-bound proof for the copied package.
- Published NHMS artifacts / display identity: selected - published root remains display-only while shared object-store receives runtime artifacts.

## Invariant Matrix

Governing invariant: A q_down publication is successful only if every published run's referenced forcing package is safely mirrored to the shared object-store with the same object-store key and verified manifest checksum.

Source-of-truth identity/contract: `hydro.hydro_run.forcing_version_id` joined to `met.forcing_version.{forcing_package_uri, checksum, lineage_json}`. `lineage_json` may contain `forcing_package_manifest_uri`, `forcing_package_manifest_checksum`, and `output_files`; these lineage fields are not DB columns. The package URI is normalized to `forcing/<source>/<cycle>/<basin_version_id>/<model_id>`.

Surfaces:

- Producers: `workers/forcing_producer/producer.py` existing forcing package writer, unchanged.
- Validators/preflight: new/updated publisher forcing key and manifest validation helpers.
- Storage/cache/query: `TilePublisher._discover_qdown_runs()` DB query plus `LocalObjectStore` source/target trees.
- Public routes/entrypoints: `TilePublisher.publish_cycle()` / `_publish_qdown_from_database()`.
- Frontend/downstream consumers: node-27 readonly object-store mirror and q_down display artifacts.
- Failure paths/rollback/stale state: copyback errors before artifact writes and publish DB commit.
- Evidence/audit/readiness: `PublishResult.lineage["object_store_copyback"]`.

Regression rows:

- Valid run with complete forcing metadata and matching manifest checksum -> shared `forcing/.../forcing_package.json` exists and run product copyback still succeeds.
- Two runs sharing one forcing package -> package is copied once and lineage lists one package entry.
- Missing `met.forcing_version`, missing package URI, missing checksum, missing manifest, checksum mismatch, traversal/absolute/wrong-prefix/symlink source, or a source key that is a regular file instead of a directory -> stable `PublishError` with run and forcing identity details; no stable q_down display artifact is advanced.
- Copyback root equals object-store root -> validate run and forcing source packages and report skipped without copying.
- Existing sibling behavior -> flood/q_down layer selection and run product copyback remain compatible.

## Risks / Trade-offs

- [Risk] Existing tests create only `hydro`/`flood` schemas. -> Add `met.forcing_version` fixture only where needed and keep missing-schema behavior explicit.
- [Risk] Manifest formats vary. -> Validate the manifest file bytes and listed same-package files only when present; do not require new fields from legacy packages.
- [Risk] Copyback lineage shape changes could break assertions. -> Preserve top-level `status`, `root`, `run_ids`, `file_count`, and `byte_count`, while adding explicit `runs` and `forcing_packages`.

## Required Test Inputs and Expected Outputs

| Case | Seeded input | Expected output |
| --- | --- | --- |
| Happy path | `hydro_run.run_id=run-a`, `forcing_version_id=forcing-1`; `met.forcing_version.forcing_package_uri=forcing/gfs/2026061400/basin-1/model-1/`; object-store has `forcing_package.json` bytes whose SHA-256 equals `checksum`, plus one listed same-package file. | `shared-object-store/forcing/gfs/2026061400/basin-1/model-1/forcing_package.json` bytes equal source; `runs/run-a/...` still copied; lineage `object_store_copyback.forcing_packages[0].object_key` is the forcing key. |
| Dedupe | `run-a` and `run-b` both reference `forcing-1` with the same normalized forcing key. | Lineage has two run entries but one forcing package entry; total package file count counted once. |
| Missing row | `hydro_run.forcing_version_id=missing-forcing` and no matching `met.forcing_version`. | `PublishError` before display artifact writes; details include `run_id=run-a`, `forcing_version_id=missing-forcing`, and missing field `forcing_version`. |
| Missing URI/checksum | Matching forcing row has blank `forcing_package_uri` or blank `checksum`. | `PublishError` details identify the missing field, run, forcing version, and no stable q_down manifest advance. |
| Integrity failure | `forcing_package.json` missing or its bytes hash differs from `checksum` or lineage `forcing_package_manifest_checksum`. | `PublishError` details include normalized forcing object key and checksum mismatch/missing manifest reason. |
| Unsafe source | URI normalizes to `../forcing`, `/tmp/forcing`, `runs/run-a`, wrong segment count, empty segment, symlink-backed source, or `forcing/.../<model_id>` exists as a regular file. | `PublishError` details include `run_id`, `forcing_version_id`, `object_key` when known; neither shared object-store nor published root receives a forcing package. |
| Exact-root skip | `NHMS_OBJECT_STORE_COPYBACK_ROOT == OBJECT_STORE_ROOT` with complete run and forcing source trees. | Copyback reports skipped for exact-root while validating both run and forcing trees. |
