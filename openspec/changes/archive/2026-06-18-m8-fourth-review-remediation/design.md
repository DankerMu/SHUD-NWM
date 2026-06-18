## Context

M7 addressed several production-readiness findings, but the fourth review exposed remaining cases where tests can pass while production behavior is false-positive:

- `run_shud_forecast_array.sbatch` invokes `nhms-shud-runtime execute --manifest-index`, and the runtime CLI resolves each index entry back to `runs/{run_id}/input/manifest.json`; the cycle orchestrator currently writes the index but does not prove that every target run manifest exists.
- `POST /api/v1/runs/{run_id}/retry` returns queued metadata after creating a pending `ops.pipeline_job`, but the current system does not submit that pending job or define a durable consumer.
- `publish_tiles.sbatch` exits successfully even though `nhms-pipeline publish-tiles` returns `{"status":"skipped","reason":"publish_tiles_not_implemented"}`.
- Flood endpoints gate on `hydro_run.status == 'frequency_done'`, which rejects `published` runs even when return-period data exists.
- `met.best_available_selection` and `BestAvailableManager.upsert_selection()` use a global `(valid_time, variable)` key that can overwrite selections across models, basins, or forcing versions.
- Data adapters accept caller-provided forecast hours without validating source-specific lead ranges and step rules.
- `LocalObjectStore.normalize_key()` can accept an `s3://` URI from a different bucket/prefix when `OBJECT_STORE_PREFIX` is configured, because it strips the bucket and keeps only the path.
- OpenAPI and OpenSpec are not trustworthy enough as release contracts: M4 strict validation fails, `SuccessEnvelope.data` conflicts with array payloads, and `issue_time=latest` is undocumented.

## Decisions

### 1. Forecast Array Runtime Contract Is Manifest-First

Before a forecast array stage can be submitted, each active basin task must have one of the following:

- A persisted per-run runtime manifest at `WORKSPACE_ROOT/runs/{run_id}/input/manifest.json`.
- A manifest-index entry that contains a complete runtime manifest and a runtime CLI path that executes it without requiring a separate file.

The preferred implementation is to create per-run `ForecastRunContext` records and manifests before forecast execution because existing runtime and parser workers already use the `runs/{run_id}` workspace convention. The array manifest index should include `run_id`, `model_id`, `basin_version_id`, `river_network_version_id`, `workspace_dir`, `object_store_root`, `object_store_prefix`, and `manifest_path`.

### 2. Retry Must Transition From Queued To Submitted

Manual retry cannot stop at inserting `status='pending'`. The implementation must choose one explicit execution path:

- Synchronous/asynchronous retry submission through the orchestrator or Slurm gateway, updating `slurm_job_id`, `submitted_at`, and events; or
- A durable pending-job consumer that selects pending retry jobs, submits them, records ownership/lease metadata, and prevents duplicate consumers from submitting the same retry twice.

If no execution path is available, the API must not return success. Pending retry jobs must not permanently block active guards.

### 3. Publish Success Requires Evidence

The publish stage must not count no-op success as a completed delivery. Acceptable release behavior is:

- Implement real publication to the selected delivery store/table and assert generated artifacts, or
- Disable the stage for the current release with an explicit skipped terminal state that is documented and not reported as product publication, or
- Return non-zero / `failed_publish` while the command is not implemented.

Tests must assert publication side effects rather than command existence.

### 4. Product-Ready Flood Runs Are Readable

Flood alert and flood map APIs must read any run state that represents a completed return-period product. At minimum this includes `frequency_done` and `published`. If future states are added, the readable-state set must be named and tested.

### 5. API And Spec Contracts Must Be Strictly Executable

OpenAPI should not define contradictory schemas. A shared success envelope may include `request_id` and `status`, but endpoint-specific `data` schemas must remain authoritative. `issue_time` must document both `latest` and ISO datetime values.

M4 OpenSpec deltas must use `### Requirement:` headings so `openspec validate --strict` can parse them. Delivery evidence referenced by README/ROADMAP must be tracked or explicitly excluded before issue closure.

### 6. Data Integrity Is Scoped By Domain Dimensions

Best-available selection must not be a global `(valid_time, variable)` singleton unless the system explicitly implements and documents a global product aggregation rule. The preferred implementation is to key selections by `forcing_version_id` or by the model/basin/source dimensions required to reproduce lineage.

Data-source manifest builders must validate caller-provided forecast hours before path generation:

- GFS hours must be non-negative, within configured max lead, and aligned to the source step.
- IFS hours must follow the configured step and respect 144h for 06/18Z and 168h for 00/12Z.
- ERA5 hourly analysis manifests must only accept 0 through 23.

When an object store prefix is configured, S3-style URIs must match that configured prefix. Bare object keys remain valid after normal storage layout validation.

## Risks / Trade-offs

- **Creating per-basin hydro runs before forecast submission changes orchestration semantics** -> Mitigate with idempotent run creation and clear conflict handling for existing run IDs.
- **Retry consumers can duplicate work** -> Mitigate with row locks, active-job checks, and idempotent Slurm submission metadata.
- **Failing publish may reduce apparent pipeline success rate** -> This is intentional; a no-op publish is not a successful release artifact.
- **OpenAPI envelope changes can affect generated clients** -> Regenerate frontend types and add schema validation tests in the same remediation.
- **Best-available key migration can affect existing rows** -> Add a forward migration with a deterministic backfill strategy or explicitly clear/rebuild derived selections.
- **Stricter URI validation can expose bad test fixtures** -> Update fixtures to use matching prefixes or bare keys instead of accepting mismatched S3 buckets.

## Migration Plan

1. Add failing characterization tests for forecast array runtime manifest availability, manual retry execution, publish no-op success, `published` flood runs, OpenAPI envelope arrays, `issue_time=latest`, and M4 strict validation.
2. Fix Slurm forecast runtime manifest preparation first because downstream forecast/parse/frequency stages depend on run-level artifacts.
3. Fix retry execution and active-guard semantics.
4. Fix publish behavior and tile/delivery evidence.
5. Fix flood readable states and API/OpenAPI contract drift.
6. Fix best-available dimensional keys, forecast-hour validation, and object-store prefix isolation.
7. Repair M4 OpenSpec formatting and repository traceability.
8. Run strict OpenSpec validation, backend regression, frontend type generation/checks, and GitHub issue traceability.

Rollback strategy: keep any schema additions forward-compatible; if publish is not ready, prefer explicit `failed_publish` or documented skipped state over pretending success.
