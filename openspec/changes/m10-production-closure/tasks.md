## 1. Real Slurm + SHUD Workload Closure

- [ ] 1.0 Record preflight: cluster/account/partition, shared workspace root, solver binary/module, selected Basins model, allowed walltime, and evidence root.
- [ ] 1.1 Add opt-in real Slurm validation command/runbook that records cluster, account, partition, node, `sacct`, shared log path, and environment metadata with secrets redacted.
- [ ] 1.2 Add a production SHUD workload template under canonical `infra/sbatch` paths, using manifest-driven inputs, configured `cpus_per_task`, `SHUD_THREADS`, walltime, object/workspace roots, and shared stdout/stderr.
- [ ] 1.3 Run at least one Basins-backed SHUD smoke/workload through real Slurm and persist workspace, logs, exit code, runtime, and output/QC evidence.
- [ ] 1.4 Validate job array behavior with at least one successful and one controlled failing task; prove partial success, retry, cancel, and log retrieval semantics.
- [ ] 1.5 Surface real Slurm job metadata through existing monitoring/API contracts or documented DB evidence.
- [ ] 1.6 Document validation command and evidence files under `artifacts/production-closure/<run_id>/slurm/`, including redacted config, `sacct` output, stdout/stderr paths, run manifest, QC result, and fast-regression commands.

### Issue #147 Evidence Map

- [ ] 1.E1 Preflight artifact: input fixture sets cluster/account/partition, shared workspace root, solver binary/module, selected model/package URI, walltime/resource profile, and evidence root; expected output is a redacted JSON/Markdown evidence file under `artifacts/production-closure/<run_id>/slurm/` with stable missing-input errors and no secret values.
- [ ] 1.E2 Shared-log template rendering: input manifest includes workspace/object roots, run_id/model_id, `cpus_per_task`, `SHUD_THREADS`, memory, walltime, and log paths; expected output renders canonical `infra/sbatch` script with shared stdout/stderr paths, explicit runtime resources, no inline secrets, and worker command using the manifest/index contract.
- [ ] 1.E3 Slurm accounting evidence parser: input fake or real `sacct` rows include job ID, state, exit code, elapsed, node list, partition, and array task rows; expected output records those fields in the evidence bundle and maps terminal failures to stable error codes.
- [ ] 1.E4 Controlled array partial success: input array has one success task and one controlled failing task; expected output preserves publishable success metadata while the failed task records task ID, stderr path, retry count, failure stage, and non-success error code.
- [ ] 1.E5 Retry/cancel evidence: input retryable Slurm failure and explicit cancel action; expected output shows retry/cancel state through existing monitoring/API or persisted DB evidence without corrupting successful task outputs.
- [ ] 1.E6 Redaction regression: input config/log/manifest/evidence values include token/password/signed-URL-shaped strings; expected output redacts the sensitive values across logs, manifests, API/evidence payloads, and docs/PR evidence.
- [ ] 1.E7 Malformed SHUD output/QC failure: input completed Slurm task with malformed `.rivqdown`, NaN/Inf values, missing required output, or count/time mismatch; expected output blocks downstream frequency/tile/API publication for that task with stable error metadata, keeps successful sibling task outputs intact, and emits no corrupted success evidence.
- [ ] 1.E8 Fast-regression commands: expected passing commands include `openspec validate m10-production-closure --strict --no-interactive`, `uv run ruff check .`, targeted Slurm/orchestrator/runtime tests, and a documented opt-in `NHMS_RUN_PRODUCTION_CLOSURE=1 ... validate-slurm --evidence-root ...` command that is skipped or reports a clear blocker when real Slurm preflight inputs are absent.

### Issue #147 Deferred Risk Packs

- [ ] 1.D1 Defer production object-store copied-root migration to #148; #147 only records package/object URI references needed by Slurm workload evidence.
- [ ] 1.D2 Defer live meteorology source discovery, download, canonical conversion, and forcing QC to #149/#150; #147 only preserves source/cycle fields already present in manifests.
- [ ] 1.D3 Defer national-scale MVT/query/frontend performance to #151; #147 only validates Slurm workload and job-array evidence.
- [ ] 1.D4 Defer full production auth/RBAC/alert/rollback readiness to #152; #147 still covers redaction and retry/cancel evidence for its Slurm lane.

## 2. Production Object Store + Basins Migration Closure

- [ ] 2.0 Record preflight: object store target, endpoint/root/prefix, credential source, cleanup policy, copied Basins root, and selected model/version.
- [ ] 2.1 Define production object-store configuration contract for endpoint/root/prefix, credentials, path containment, redaction, and local/S3 parity.
- [ ] 2.2 Reuse M9 migration-report capability to produce production evidence from a copied Basins root; symlink-only roots must fail production readiness.
- [ ] 2.3 Reuse M9 package publication against a production-like object store and verify manifest/object checksums from stored bytes.
- [ ] 2.4 Import published package into registry and prove model/API/runtime consumption uses object URIs rather than development source paths.
- [ ] 2.5 Add cleanup/rollback and conflict evidence for partially failed publish/import operations.
- [ ] 2.6 Document validation command and evidence files under `artifacts/production-closure/<run_id>/object-store/`, including migration report, package manifest, registry import report, API/runtime smoke, cleanup report, and fast-regression commands.

## 3. Live Meteorology Ingestion + QC Closure

- [ ] 3.0 Record preflight: enabled source subset, credentials/public path, cached fallback policy, cycle window, object prefix, and CLDAS restricted reason if not enabled.
- [ ] 3.1 Add redacted source configuration templates for GFS, IFS, ERA5, and restricted CLDAS with explicit enabled/skipped status.
- [ ] 3.2 Run live or production-like cycle discovery/download for available GFS/IFS/ERA5 sources with retry, checksum, file count, and manifest evidence.
- [ ] 3.3 Convert downloaded source data to canonical products and persist raw/canonical lineage.
- [ ] 3.4 Produce forcing for at least one Basins-backed model and run forcing QC for continuity, units, missing values, and variable ranges.
- [ ] 3.5 Record best-available lineage and skipped/restricted source reasons without fabricating CLDAS success.
- [ ] 3.6 Document validation command and evidence files under `artifacts/production-closure/<run_id>/met/`, including redacted source config, raw/canonical manifests, forcing manifest, QC result, best-available lineage, and fast-regression commands.

## 4. Staging End-to-End Closure

- [ ] 4.0 Record preflight: source cycle, model set, DB target, object prefix, Slurm partition/account, frontend API base, and evidence root.
- [ ] 4.1 Create a staging E2E runbook and command that selects source cycle, model set, object prefix, Slurm partition, and DB target explicitly.
- [ ] 4.2 Run `download -> canonical -> forcing -> Slurm SHUD -> parse -> flood frequency -> tile publish` for a bounded model set.
- [ ] 4.3 Verify API surfaces by starting from the closure evidence root, then querying existing contracts with derived `model_id`, `basin_version_id`, `segment_id`, `source/cycle_time`, `job_id`, and `layer_id`; only add run_id-specific API filters if the issue explicitly implements that contract.
- [ ] 4.4 Verify frontend smoke or Playwright flow loads the published run with no mock data and shows source/model/run lineage.
- [ ] 4.5 Enforce SHUD output QC before downstream publication: malformed `.rivqdown`, NaN/Inf, missing required outputs, or count/time mismatches must fail with stable error codes and block frequency/tile/API publication for that run.
- [ ] 4.6 Emit a closure evidence bundle mapping every artifact to run_id, source cycle, model/version, Slurm job, QC result, and object URI.

## 5. National-Scale MVT / Performance Closure

- [ ] 5.0 Record preflight: real imported dataset or deterministic fixture source, minimum segment/model counts, bbox sizes, thresholds file, and tile content-type expectation.
- [ ] 5.1 Build or select a national-scale river network fixture from imported Basins/registry data and document segment counts and geometry bounds.
- [ ] 5.2 Implement or validate true MVT publication when `application/x-protobuf` production delivery is required; otherwise close only with an explicit release-blocking report that lists affected endpoints, missing implementation work, and states production tile readiness is not achieved.
- [ ] 5.3 Capture PostGIS query plans and p95 latency for model listing, river segments bbox, flood alerts, forecast series, pipeline jobs, and tile metadata.
- [ ] 5.4 Capture frontend load/render evidence for large river layers and timeline/chart interactions at desktop and mobile breakpoints.
- [ ] 5.5 Define capacity thresholds and failure behavior for oversized tiles, bbox queries, long time ranges, and object-store artifact listings.
- [ ] 5.6 Document validation command and evidence files under `artifacts/production-closure/<run_id>/scale/`, including dataset manifest, threshold file, query plans, latency report, tile report, frontend screenshots/results, and fast-regression commands.

## 6. Production Ops / Security / Runbook Readiness

- [ ] 6.0 Record preflight: auth mode, required roles, alert target or dry-run sink, deployment config source, and rollback drill scope.
- [ ] 6.1 Add production environment templates and validation docs for API/orchestrator/slurm-gateway/tile/frontend services.
- [ ] 6.2 Enforce backend-side auth/RBAC boundary for operator/model-admin actions or document a release-blocking fallback; required actions include model activation, rerun, cancel, QC override, source config change, and tile republish.
- [ ] 6.3 Add secret redaction tests for logs, manifests, audit rows, API payloads, PR evidence, and frontend output.
- [ ] 6.4 Add monitoring/alert evidence for source latency, Slurm queue backlog, failed basin retries, object-store failures, stale analysis state, tile errors, and API p95.
- [ ] 6.5 Add rollback drill documentation for bad model activation, failed publish/import, failed source cycle, failed Slurm array, and bad tile release.
- [ ] 6.6 Update `progress.md` and `docs/VALIDATION.md` when each production closure evidence lane lands.

## Issue Dependencies

- Issues 1, 2, and 3 may start independently after their preflight inputs are available.
- Issue 4 depends on minimum accepted evidence from Issues 1, 2, and 3.
- Issue 5 depends on Issue 2 and either real imported national data or an approved deterministic large fixture.
- Issue 6 may start early, but final acceptance depends on evidence and rollback surfaces from Issues 1-5.
