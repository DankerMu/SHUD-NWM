## Context

Issue #124 is part of Epic #120 and is unblocked by #121. #122 and #123 are already merged. Current production risk is that mock orchestration can succeed while real Slurm fails because stage/job type/template contracts diverge across Forecast M3, Analysis, legacy Forecast, and Hindcast.

Fixture level: expanded
Project profile: other

Change surface:
- Orchestrator stage definitions and submission payloads: `services/orchestrator/chain.py`
- Real gateway template/config mapping: `services/slurm_gateway/config.py`, `services/slurm_gateway/real_backend.py`, `config/job_type_templates.yaml`
- Production templates: `infra/sbatch/`
- Legacy templates/docs: `workers/sbatch_templates/README.md`, possibly `workers/sbatch_templates/*.sbatch`
- Hindcast forcing/runtime path: `workers/flood_frequency/hindcast.py`
- Tests: `tests/test_analysis_pipeline.py`, `tests/test_orchestrator.py`, `tests/test_real_slurm_gateway.py`, `tests/test_job_array.py`, `tests/test_hindcast.py`

Must preserve:
- M3 Forecast production path keeps using canonical `infra/sbatch` templates.
- `publish_tiles` behavior from #122 remains intact.
- Real gateway must continue using Jinja `StrictUndefined` and sandboxed rendering.
- Existing mock orchestration tests keep validating stage ordering and failure propagation.
- Hindcast successful coverage path and API response shape remain compatible.
- Object store root/prefix exports remain present in production templates.

Must add/change:
- Analysis stages must use real gateway-recognized job types and canonical production templates. Expected production mapping:
  - `analysis_download_source_cycle` -> `analysis_download_source_cycle.sbatch`
  - `analysis_convert_canonical` -> `analysis_convert_canonical.sbatch`
  - `analysis_produce_forcing` -> `analysis_produce_forcing.sbatch`
  - `run_shud_analysis` -> `run_shud_analysis.sbatch`
  - `parse_analysis_output` -> `parse_analysis_output.sbatch`
  - `save_state_snapshot` -> `save_state_snapshot.sbatch`
  If implementation chooses different final names, it must document the mapping in tests and keep the names Analysis-specific enough to avoid ambiguity with Forecast M3 stages.
- `config/job_type_templates.yaml` and `SlurmGatewaySettings.DEFAULT_JOB_TYPE_TEMPLATES` must include every production job type needed by Forecast M3, Analysis, and Hindcast.
- Tests must prove real gateway can render/submit Analysis and Hindcast production templates with fake Slurm binaries or monkeypatched subprocess calls.
- Fake Slurm evidence must explicitly cover `sbatch`, `sacct`, `scancel`, and `sinfo`, and must exercise submit, status, array task status, logs, and cancel across production job types. Existing tests can satisfy part of the matrix, but the issue #124 implementation must name/extend the tests that provide the evidence.
- Legacy `workers/sbatch_templates` status must be explicit so production code does not depend on it silently. In particular, `OrchestratorConfig.templates_dir` and `_submit_and_wait`/rendered `script` behavior must be changed or documented so `workers/sbatch_templates` is non-production/test-only and real gateway production execution uses mapped `job_type` templates rather than manifest `script` content.
- Hindcast metadata-only forcing fallback must not proceed into SHUD runtime; runtime manifests must require real forcing package context. The failure contract must use a stable error code such as `HINDCAST_FORCING_PACKAGE_UNAVAILABLE` and tests must assert whether that code is raised directly, persisted to `hydro.hydro_run.error_code`, or surfaced through the API response.

## Goals / Non-Goals

**Goals:**
- Make real Slurm production execution paths explicit, consistent, and regression-tested.
- Prevent mock-only success for Analysis/Hindcast.
- Make template mapping drift fail in tests.

**Non-Goals:**
- Build the full real database/e2e matrix from #126.
- Replace all legacy tests that intentionally exercise mock orchestration.
- Change frontend production behavior from #125.
- Redesign Slurm accounting or retry semantics beyond issue #124 scope.

## Decisions

### 1. Canonical Production Templates

Production Slurm templates live in `infra/sbatch`. New production job types should be mapped there rather than to `workers/sbatch_templates`.

### 2. No Silent Rendered Script Payloads

RealSlurmGateway renders templates by `job_type`; it does not execute arbitrary `script` manifest payloads. If a caller still includes `script`, it must be ignored safely or rejected/documented, and tests must prove production paths do not rely on it.

For issue #124, the target production behavior is: Analysis/Hindcast production submissions use mapped `job_type` values and template variables. Rendered `script` payloads may remain only for legacy/mock tests and must not be the mechanism required for real Slurm execution.

### 3. Hindcast Requires Real Forcing

Metadata-only hindcast forcing is acceptable only as a non-runtime placeholder/error path. A hindcast run must not enter SHUD runtime unless a real forcing package URI or producer result is available and persisted.

## Risk Packs Considered

- Public API / CLI / script entry: selected - Slurm job submission and worker commands are execution entrypoints.
- Config / project setup: selected - job type mapping and template directory defaults change.
- File IO / path safety / overwrite: selected - templates, manifests, logs, object store paths, and workspace files are affected.
- Schema / columns / units / field names: selected - manifests and DB status/error fields carry run/job contracts.
- Geospatial / CRS / shapefile sidecars: not selected - no geometry transformation intended.
- Time series / forcing / temporal boundaries: selected - Analysis/Hindcast forcing windows and year bounds are central.
- Numerical stability / conservation / NaN: not selected - no hydrologic numerical algorithm change intended.
- Solver runtime / performance / threading: selected - SHUD runtime templates and resource profiles are involved.
- Resource limits / large input / discovery: selected - Slurm arrays, max concurrency, and fake binary tests cover resource controls.
- Legacy compatibility / examples: selected - legacy template directory and legacy Forecast path must be handled deliberately.
- Error handling / rollback / partial outputs: selected - stage failure, timeout, and metadata-only forcing guards are required.
- Release / packaging / dependency compatibility: selected - production deployment depends on packaged templates/config.
- Documentation / migration notes: selected - legacy/production template boundary must be documented.

Selected risk packs:
- Public API / CLI / script entry
- Config / project setup
- File IO / path safety / overwrite
- Schema / columns / units / field names
- Time series / forcing / temporal boundaries
- Solver runtime / performance / threading
- Resource limits / large input / discovery
- Legacy compatibility / examples
- Error handling / rollback / partial outputs
- Release / packaging / dependency compatibility
- Documentation / migration notes

## Risks / Trade-offs

- Adding production Analysis templates can duplicate legacy template intent. Mitigation: keep templates thin and map job types explicitly.
- Tightening Hindcast forcing guards can change behavior for tests that used metadata-only fallback. Mitigation: update tests to distinguish producer-unavailable failure from real forcing success.
- Template mapping changes can break existing fake gateway tests. Mitigation: add mapping tests and keep mock-client behavior compatible.

## Migration Plan

1. Characterize current Forecast/Analysis/Hindcast job type/template/manifest contracts in tests.
2. Add canonical Analysis/Hindcast mappings/templates or align existing templates to the canonical mapping.
3. Add fake real-gateway tests for template rendering/submission/status/log/cancel coverage.
4. Tighten Hindcast forcing guard and update tests.
5. Update legacy template README and mapping assertions.

Rollback strategy: revert orchestrator template mapping and Hindcast guard changes together; do not leave real gateway mapping without matching templates/tests.

## Review Focus

- No production code path relies on `workers/sbatch_templates` or ignored `script` payloads.
- Every real gateway job type has a template in `infra/sbatch` and config mapping.
- Analysis/Hindcast manifests contain required run/model/source/time/object-store/resource fields.
- Fake Slurm tests exercise submit/status/array-task-status/log/cancel, `sbatch`/`sacct`/`scancel`/`sinfo`, and template rendering.
- Metadata-only Hindcast forcing cannot enter SHUD runtime and exposes/persists a stable error code.
