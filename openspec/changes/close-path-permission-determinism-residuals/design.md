## Context

This is the explicit follow-up for `path-expansion-throw-face-closure` tasks D.4, D.11, and D.12(a), plus its Basins sibling #1554. The common defect is not a single primitive: errno-less home expansion, strict real-path resolution, metadata classification, and required-file health require different lane semantics. CPython 3.11 propagates permission errors from several `pathlib` predicates that later versions convert to `False`, so those predicates cannot define a supported-version contract.

Fixture level: expanded. Repair intensity: high. The change touches write-side paths, permission/error boundaries, shared production entrypoints, and existing compatibility behavior.

## Goals / Non-Goals

**Goals:**

- Preserve one governing invariant: path expansion, resolution, and classification failures are translated at the owning module boundary, with the same public verdict on every supported CPython; an undeterminable write-side path never becomes a literal `~` path, and permission denial is never labelled as a symlink defect.
- Exercise real public seams: chain log publication, `ProductionMetConfig.from_env`, `validate_met`, scheduler final-component validation, `discover_basins_inventory`, and migration-report creation.
- Preserve existing payload keys and successful-path behavior.

**Non-Goals:**

- Do not modify archived OpenSpec changes, `packages/common/safe_fs.py`, `scheduler_config.py`, database behavior, Slurm dispatch, or frontend/display paths.
- Do not sweep unrelated bare `expanduser()`/`resolve()` sites in workspace paths, met symlink-component probes, `resolve_basins_root`, or scripts.
- Do not change dangling-symlink, containment, symlink-loop, or symlink-to-file scheduler verdicts except the specific denied-traversal classification after fallback.

## Decisions

### 1. Translate at each lane owner; do not add a universal helper

`_absolute_configured_path` will narrow errno-less expansion failure to `SafeFilesystemError(kind="unsafe")`, matching the shared write-side primitive contract. All three chain consumers must include path expansion inside their existing structured-error boundary; `published_log_path` will add the same `PUBLISHED_LOG_WRITE_FAILED` translation because it currently has no boundary. The helper keeps its `Path -> Path` signature and `chain.py` compatibility export.

`_safe_resolved_evidence_root` will directly translate expansion failure to `ProductionMetValidationError("PRODUCTION_MET_EVIDENCE_PATH_UNSAFE", ...)`, because that module owns the evidence error taxonomy. Reusing a permissive config-construction fallback was rejected: both lanes feed writes, so keeping a literal tilde can target a path the operator did not name.

### 2. Separate scheduler resolution fallback from target classification

The strict-realpath step keeps the predecessor contract: `ELOOP` is refused and every other strict-resolution error falls back to non-strict realpath so dangling and `ENOTDIR` compatibility remain intact. After containment is rechecked, the final target is classified through explicit metadata access rather than `Path.exists() and not Path.is_dir()`.

A target that is provably absent remains accepted as before. A target that is a directory remains accepted, a non-directory keeps `must be a directory`, and `EACCES`/`EPERM` fails closed through the existing `ValueError` safety family rather than leaking `PermissionError` or being treated as absent. Silent acceptance was rejected because denied traversal means the guard cannot prove the configured target is a directory.

### 3. Basins root probes use errno-aware metadata once

Discovery and migration-report roots will classify one explicit metadata probe: `ENOENT`/`ENOTDIR` maps to `BASINS_ROOT_NOT_FOUND`; `EACCES`/`EPERM` maps to `BASINS_ROOT_UNREADABLE`; other metadata failures remain structured under the unreadable root contract. Root symlink identity is read without returning to version-dependent `Path.is_symlink()` after the root has passed validation. `_ensure_readable_directory` must place its directory-kind probe inside the same structured boundary.

### 4. Required-file EACCES uses the existing third state

`_safe_resolve_under_root` must keep `ENOENT` as silent nonexistence, `ELOOP` and other actual resolution defects as `BASINS_SYMLINK_UNRESOLVABLE`, and outside-root targets as `BASINS_SYMLINK_OUTSIDE_ROOT`. `EACCES`/`EPERM` is not a symlink verdict: it receives a non-blocking `BASINS_PATH_UNREADABLE` warning and must remain reachable to the required-file stat/hash lane, which records `BASINS_REQUIRED_FILE_UNREADABLE`, `unreadable_required_files`, and `partial` status. This follows the existing third-state requirement; it does not add a payload field or make a partial model publishable.

The implementation may carry a small resolution result or explicit permission flag rather than returning `None` for permission denial. It must not weaken containment: a path whose containment cannot be established is not admitted as safe merely to reach checksum handling.

### 5. Tests use deterministic errno injection plus a real 3.11 lane

Primary tests inject `RuntimeError`, `PermissionError(EACCES)`, or `OSError` at the narrow owning primitive, avoiding chmod behavior that varies with root, ACLs, and macOS. Geometry tests retain existing symlink cases. New behavior tests must include a batched red proof against pre-change production source and mutant checks for permissive tilde retention, `Path.exists()` regression, and EACCES-as-symlink regression.

A separate environment runs the focused scheduler/Basins suite under Python 3.11 using `UV_PROJECT_ENVIRONMENT=/tmp/venv311-1621-path-permission`; the project `.venv` is never rebuilt for that interpreter. These changes are db-free, so local focused tests, ruff, OpenSpec validation, and the isolated 3.11 matrix are the required oracle. Node-27 is not a mandatory live receipt for this scope; node-22 is not used.

## Invariant Matrix

- **Governing invariant:** Every supported interpreter produces the same owning-module verdict for path expansion, metadata, and permission failures; write-side tilde failures have no filesystem side effect, and EACCES is not represented as nonexistence or a symlink defect.
- **Source-of-truth identity/contract:** configured path plus kernel errno (`ENOENT`/`ENOTDIR`, `EACCES`/`EPERM`, `ELOOP`) and each module's existing structured error/status taxonomy.
- **Producers:** `NHMS_PUBLISHED_ARTIFACT_ROOT`, production-met `evidence_root`, scheduler configured directory paths, explicit Basins root and matched required files.
- **Validators/preflight:** chain workspace publication helpers, `_safe_resolved_evidence_root`, `_require_safe_directory_final_component`, Basins root/readability and strict-resolution helpers.
- **Storage/cache/query:** local evidence/log writes and Basins inventory payload; no database/cache changes.
- **Public routes/entrypoints:** chain log publication, `ProductionMetConfig.from_env`, `validate_met`, scheduler config guard, `discover_basins_inventory`, `write_basins_migration_report`.
- **Frontend/downstream consumers:** orchestrator error handlers, production-met CLI receipt, Basins import/package consumers; payload keys remain unchanged.
- **Failure paths/rollback/stale state:** no literal tilde creation, no partial evidence/log write on expansion refusal, no raw `RuntimeError`/`PermissionError`, no EACCES symlink warning.
- **Evidence/audit/readiness:** focused pytest, isolated CPython 3.11 pytest, full tracked-Python ruff, strict OpenSpec validation, PR review evidence.
- **Regression rows:**
  - valid absolute roots and readable required files -> existing paths, checksums, status, and publication behavior remain unchanged;
  - unknown-user tilde -> owning structured error and no literal `~` entry;
  - scheduler target under a denied-traversal parent -> structured `ValueError`, never raw permission error or acceptance;
  - Basins root permission denial -> `BASINS_ROOT_UNREADABLE`; required-file permission denial -> `unreadable_required_files` plus `partial`, never `SYMLINK_*`;
  - loop, dangling-inside, dangling-outside, symlink-to-file, healthy-directory, and `ENOTDIR` target-through-regular-file siblings -> existing scheduler and Basins compatibility verdicts remain unchanged; `ENOTDIR` still takes the non-loop fallback and never becomes a loop or permission refusal.

## Boundary-Surface Checklist

- Shared helper roots: no change to `safe_fs`; lane-local translations only.
- Public entrypoints: all six named seams require direct tests.
- Read/stat surfaces: scheduler target metadata and Basins root/required-file metadata use errno-aware boundaries.
- Write surfaces: chain and met must reject before any directory or file is created.
- Producer/consumer evidence boundaries: structured codes/status collections keep existing schemas.
- Unchanged consumers: chain facade, Basins package/import, and scheduler sibling geometries remain compatible.

## Risk Packs Considered

- Public API / CLI / script entry: selected — failures are observed at production command/orchestrator entrypoints.
- Config / project setup: selected — configured roots are the trigger inputs.
- File IO / path safety / overwrite: selected — write-side tilde and permission probes govern filesystem access.
- Schema / columns / units / field names: selected — preserve Basins inventory keys and existing error-code taxonomy.
- Auth / permissions / secrets: selected — filesystem permission denial is the core boundary; no credential handling changes.
- Concurrency / shared state / ordering: not selected — no concurrent state transition changes.
- Resource limits / large input / discovery: selected — Basins discovery behavior and existing bounds must remain intact.
- Legacy compatibility / examples: selected — CPython 3.11 and established symlink verdicts must remain supported.
- Error handling / rollback / partial outputs: selected — no raw exceptions or partial writes; third-state health remains explicit.
- Release / packaging / dependency compatibility: not selected — no dependency or packaging metadata changes.
- Documentation / migration notes: selected — live specs and HONEST LIMIT text must reflect closure; no deployment migration.
- Geospatial / CRS / basin geometry: not selected — no geometry or CRS behavior changes.
- Hydro-met time series / forcing windows: not selected — met evidence path only, no forcing data semantics.
- SHUD numerical runtime / conservation / NaN: not selected — no numerical behavior.
- PostGIS / TimescaleDB domain behavior: not selected — db-free.
- Slurm production lifecycle / mock-vs-real parity: selected — scheduler runtime-root preflight semantics, not Slurm submission.
- External hydro-met providers / snapshot reproducibility: not selected — no provider data flow.
- Run manifest / QC provenance: not selected — no manifest/QC identity changes.
- Published NHMS artifacts / display identity: selected — published log/evidence roots must fail before wrong-path writes.

## Risks / Trade-offs

- **Denied scheduler targets change from later-Python acceptance to refusal** → This is an intentional fail-closed correction anchored by #1623; sibling compatibility scenarios remain explicit and tested.
- **Basins resolution may know EACCES before it can prove containment** → Keep containment fail-closed; only route to the third state when the lexical path is under an already trusted root and no resolution evidence shows escape.
- **Monkeypatch tests can target the wrong layer** → Patch one named primitive per test and retain real geometry plus the isolated 3.11 suite.
- **Combined PR spans multiple modules** → One invariant, disjoint lane changes, a high-risk six-reviewer pass, and class-level verification keep the shared contract auditable.

## Migration Plan

No data migration is required. Deploy as a normal code update; rollback is the parent commit because no persisted schema changes. Archived predecessor artifacts remain untouched; this change's deltas update the live specs on archive.

## Open Questions

None. The existing Basins third-state requirement resolves #1554's prior triage question, and fail-closed scheduler classification resolves #1623 without weakening the directory guard.