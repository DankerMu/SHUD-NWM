# Risk Triage

- Issue type: bugfix; project profile: NHMS; blast radius: high.
- Fixture level: expanded; repair intensity: high.
- Upstream suggested level: absent. Minimal mergeable slice: absent; the combined slice is the four named residual lanes plus their existing public tests/spec deltas.
- Selected packs: Public API / CLI, Config, File IO / path safety, Schema / field names, Auth / permissions, Resource/discovery limits, Legacy compatibility, Error handling / partial outputs, Documentation, Slurm lifecycle, Published artifacts.
- Not selected: Concurrency, Release/dependencies, Geospatial/CRS, forcing windows, numerical runtime, PostGIS/TimescaleDB, external providers, manifest/QC provenance; none of those contracts change.

## 1. Structured expansion refusals (#1621, #1622)

- [x] 1.1 Make `_absolute_configured_path` translate undeterminable-home expansion to `SafeFilesystemError(kind="unsafe")`, document the contract, and keep cwd anchoring and the `Path -> Path` compatibility surface unchanged.
- [x] 1.2 Put all three chain consumers (`persist_gateway_logs`, `write_local_stage_log`, `published_log_path`) inside the existing `PUBLISHED_LOG_WRITE_FAILED` boundary, with no partial write or literal `~` path.
- [x] 1.3 Make `_safe_resolved_evidence_root` translate the same expansion failure to `PRODUCTION_MET_EVIDENCE_PATH_UNSAFE` without changing valid-root resolution.
- [x] 1.4 Add real-seam tests for the three chain operations plus `ProductionMetConfig.from_env` and `validate_met`; unknown-user tilde -> exact structured code, no bare `RuntimeError`, no cwd side effect; valid roots -> unchanged product.
- [x] 1.5 Remove only the live #1622 HONEST LIMIT text/tests now superseded by end-to-end evidence; do not edit archived changes.

## 2. Scheduler target classification (#1623)

- [x] 2.1 Replace the final `Path.exists()`/`is_dir()` gate with explicit errno-aware metadata classification: absent remains accepted, directory accepted, non-directory keeps `must be a directory`, and `EACCES`/`EPERM` becomes structured `ValueError` rather than raw exception or acceptance.
- [x] 2.2 Preserve strict-realpath `ELOOP` refusal, non-loop fallback, containment recheck, and the existing healthy/dangling/escape/file/`ENOTDIR` verdicts.
- [x] 2.3 Replace the version-conditional permission fallback test with one version-independent assertion; delete the related HONEST LIMIT comment and retain explicit sibling-geometry regressions.

## 3. Basins permission semantics (#1554)

- [x] 3.1 Normalize discovery root metadata: `ENOENT`/`ENOTDIR` -> `BASINS_ROOT_NOT_FOUND`; `EACCES`/`EPERM` and other unreadable metadata failures -> `BASINS_ROOT_UNREADABLE`; never leak `PermissionError` from root kind/symlink/readability probes.
- [x] 3.2 Apply the same root classification to `write_basins_migration_report`, translated through `BasinsPackageError`, while preserving the production symlink-target refusal.
- [x] 3.3 Move `_ensure_readable_directory` kind/stat probes inside one structured `BasinsDiscoveryError` boundary and audit the issue-named sibling predicates; leave guarded sites unchanged only with a precise guard-order rationale.
- [x] 3.4 Split `_safe_resolve_under_root` by errno: missing stays silent, actual resolution defects retain `BASINS_SYMLINK_UNRESOLVABLE`, outside-root retains `BASINS_SYMLINK_OUTSIDE_ROOT`, and `EACCES`/`EPERM` uses non-symlink unreadability semantics without weakening containment.
- [x] 3.5 Route permission-denied matched required files into `unreadable_required_files`, `BASINS_REQUIRED_FILE_UNREADABLE`, and `partial`; preserve all payload keys, valid checksums, and default publication refusal for partial models.
- [x] 3.6 Add deterministic EACCES tests at `discover_basins_inventory`, required-file processing, and migration-report entrypoints; 3.11/current Python -> identical structured codes/status and no `SYMLINK_*` misclassification.

## 4. Oracle and documentation integrity

- [x] 4.1 Prove new behavior tests bite in one batched red run against pre-change production source; leave no `red-proof` stash and record invocation/output.
- [x] 4.2 Update only the three OpenSpec deltas in this change; preserve unrelated scenarios, archived artifacts, payload schemas, test strength, and CI gates.
- [x] 4.3 Audit every Invariant Matrix row and all unchanged sibling consumers; report changed/clean/out-of-scope status and any plan deviation.

## 5. Evidence Floor

- [x] 5.1 `uv run pytest -q tests/test_orchestration_chain.py tests/test_pipeline_logs_artifacts.py tests/test_production_met_validation.py tests/test_safe_fs.py tests/test_production_scheduler.py tests/test_basins_discovery.py tests/test_basins_registry_import.py tests/test_basins_package_publication.py` -> all selected scenarios pass.
- [x] 5.2 `uv run ruff check $(git ls-files '*.py')` -> zero findings; use tracked Python files so local untracked `.claude` tooling is excluded.
- [x] 5.3 `UV_PROJECT_ENVIRONMENT=/tmp/venv311-1621-path-permission uv run --python 3.11 python -m pytest -q tests/test_production_scheduler.py tests/test_basins_discovery.py tests/test_basins_package_publication.py` -> same assertions pass without version-conditioned expectations; do not reuse the project `.venv`.
- [x] 5.4 `openspec validate close-path-permission-determinism-residuals --strict --no-interactive` -> strict valid.
- [x] 5.5 On node-27 detached worktree `/home/nwm/NWM-1621` at `4cc6206c4084136a8509f78d3afd7edd0135c39f`: Python 3.14.5 focused suite -> `2646 passed, 26 skipped`; isolated Python 3.11.15 scheduler/Basins matrix -> `2096 passed, 2 skipped`; tracked-Python ruff -> pass. Node-22 was not used because no Slurm dispatch/compute behavior changed.

## 6. Round 1 Verified Invariant Closure

- [x] 6.1 Replace Basins resolve/metadata `None` plus shared-warning control flow with an explicit per-call state; EACCES/EPERM still undergoes non-strict containment, outside targets stay blocking, every required-directory depth (model candidate, `input/`, and `input/<shud_input_name>`) hard-refuses at a shared required-directory owner, optional directories warn/skip, and matched required files alone enter the unreadable third state.
- [x] 6.2 Route GIS/glob final-stat EACCES through an errno-aware regular-file verdict; `cfg.ic`/`sp.mesh` permission denial skips content validation and appears only in `unreadable_required_files`, never missing, `invalid_required_files`, raw `PermissionError`, or `SYMLINK_*`.
- [x] 6.3 Add biting regressions for top-level, `zhaochen/WEM/input`, and `zhaochen/WEM/input/WEM` strict-resolution/final-stat EACCES, outside denied-parent candidate/forcing symlinks, symlink-root follow-stat EACCES at discovery/migration, GIS strict-resolve EACCES, GIS/SHUD final-stat EACCES, `cfg.ic`/`sp.mesh` unreadable-only state, and cross-model warning isolation; prove current and Python 3.11 convergence.
- [x] 6.4 Restore the chain local-stage unknown-home diagnostic (`Failed to publish local stage logs.`) while retaining `PUBLISHED_LOG_WRITE_FAILED`, exact `log_uri`, and direct `SafeFilesystemError(kind="unsafe")` cause; gateway/direct/full-cycle diagnostics remain unchanged.
- [ ] 6.5 Produce a batched red proof for the new Round 1 closure tests, run the full eight-file suite, tracked-Python ruff, strict OpenSpec, isolated Python 3.11 matrix, Phase 6.2 invariant audit, and a final-head node-27 detached receipt before merge.
