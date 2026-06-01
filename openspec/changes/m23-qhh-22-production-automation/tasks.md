## 0. Change Preflight and Current-State Evidence

- [ ] 0.1 Validate this OpenSpec change with `openspec validate m23-qhh-22-production-automation --strict --no-interactive` before implementation issues begin.
- [ ] 0.2 Capture redacted node-22 baseline evidence: service role, roots, SHUD executable status, Slurm health, active model count, canonical product count, forcing count, hydro count, pipeline job/event count, and QHH package file presence.
- [ ] 0.3 Record in the Epic that QHH package files exist and that the verified gap is missing production DB bootstrap plus dynamic per-cycle forcing, not missing static watershed extraction.
- [ ] 0.4 Record in the Epic that rSHUD/AutoSHUD is a static contract reference and SHUD is the runtime model engine.

## 1. Production Contract and Scheduler Roots

- [x] 1.1 Define the QHH production identity matrix covering `run_id`, `model_id`, `basin_id`, `basin_version_id`, `river_network_version_id`, `source`, `cycle_time`, `canonical_product_id`, `forcing_version_id`, `hydro_run`, published manifest identity, and optional pipeline job/event correlation.
- [x] 1.2 Define the production stage/status/error taxonomy for download, convert, forcing, forecast, parse, q_down publish, frequency/flood publish, and aggregate terminal states, including blocked/partial/unavailable semantics.
- [x] 1.3 Define the URI and artifact boundary for workspace, object store, published root, `published://` logs/manifests, and private path rejection.
- [x] 1.3a Add reusable contract helpers or fixtures for the M23 identity/status/URI boundary without adding live download, SHUD execution, Slurm submission, parse, or publish mutation.
- [x] 1.3b Add regression tests proving a full QHH identity tuple is accepted as same-run evidence and that mismatched `run_id`, `model_id`, `basin_id`, `source`, `cycle_time`, basin/river version, canonical product, forcing version, hydro run, manifest, or present pipeline job/event correlation is rejected.
- [x] 1.3c Add regression tests proving `published://` or allowlisted published-root URIs can be display-readable evidence only when identity-bound, while private workspace, scratch-only, Slurm-private, traversal, and non-allowlisted local paths are rejected.
- [ ] 1.4 Fix `nhms-pipeline plan-production` so omitted `--workspace-root`, lock, evidence, temporary, object-store, and published-root paths use documented environment/config defaults instead of `.nhms-workspace` under the app directory.
- [ ] 1.5 Update `infra/compose.compute.yml`, env examples, and systemd/timer docs so scheduler-once and continuous/timer modes can run with explicit roots, locks, service role, source/model filters, and evidence paths.
- [ ] 1.6 Verify with `uv run pytest -q tests/test_production_scheduler.py tests/test_production_slurm_validation.py` plus `uv run ruff check .` and a Docker `nhms-pipeline plan-production --plan` smoke without `--workspace-root`.

### Issue #252 Evidence Floor

- `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py`: identity/status/URI contract fixtures pass without live runtime mutation.
- `uv run ruff check .`: contract helper/schema changes are lint-clean.
- `openspec validate m23-qhh-22-production-automation --strict --no-interactive`: OpenSpec remains valid.
- Non-goal evidence: implementation must not add live forecast download, SHUD execution, Slurm submission, parse/publish mutation, or frontend behavior in #252.

## 2. QHH Model Bootstrap

- [ ] 2.1 Add or harden an idempotent QHH bootstrap command that imports/publishes the processed Basins package, creates or activates `core.model_instance`, and records package/manifest identity.
- [ ] 2.2 Ensure the active QHH model exposes scheduler-required `model_id`, `basin_id`, `basin_version_id`, `river_network_version_id`, `model_package_uri`, `shud_code_version`, and runnable `resource_profile` metadata.
- [ ] 2.3 Extend station seeding to validate `qhh.tsd.forc`, enforce station count, populate `met.met_station` forcing-grid rows, and report created/updated/unchanged counts.
- [ ] 2.4 Seed or validate QHH output river/segment identities required by the output parser and publisher.
- [ ] 2.5 Add tests for missing package, station count mismatch, repeated bootstrap idempotency, active model discovery with no `not_shud_model`/`incomplete_model_metadata` exclusion, and duplicate active model rejection.
- [ ] 2.6 Verify with focused bootstrap/registry tests, `uv run pytest -q tests/test_production_scheduler.py`, `uv run ruff check .`, and `nhms-pipeline plan-production --plan --model-id <qhh_model_id>` evidence.

## 3. Fresh Forecast Ingestion

- [ ] 3.1 Harden GFS/IFS cycle discovery with source-specific lag/lookback/horizon policy, 403/unavailable handling, operator filters, and typed blocked evidence.
- [ ] 3.2 Ensure download and canonical conversion require source-specific complete QHH forcing variable ids and per-valid-time lead coverage before marking canonical products ready.
- [ ] 3.3 Add idempotency and retry tests for completed canonical reuse, transient download failure, source unavailable, incomplete variables, source-specific horizon handling, and reduced-scope source filters.
- [ ] 3.4 Verify with focused adapter/canonical tests, `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py`, `uv run ruff check .`, and a dry-run evidence sample that cannot proceed to forcing when canonical coverage is incomplete.

## 4. Fixed-Station Forcing Production

- [ ] 4.1 Wire production scheduler candidates to `workers/forcing_producer` using active QHH model/basin metadata and fixed forcing-grid stations.
- [ ] 4.2 Persist one ready `met.forcing_version` and `met.forcing_station_timeseries` set per model/source/cycle/canonical identity with units, quality flags, station count, and time range.
- [ ] 4.3 Materialize SHUD-ready forcing files and runtime manifests with station ordering, filenames, checksums, units, time range, and source/cycle identity.
- [ ] 4.4 Add tests for missing fixed stations, interpolation coverage gaps, idempotent rerun, SHUD package file generation, and no rSHUD runtime dependency.
- [ ] 4.5 Verify with `uv run pytest -q tests/test_forcing_producer.py tests/test_orchestration_chain.py tests/test_production_scheduler.py`, `uv run ruff check .`, and forcing evidence containing station count, variable count, time range, and manifest checksum.

## 5. Real SHUD and Slurm Execution

- [ ] 5.1 Add scheduler/orchestrator pre-submit runtime preflight that rejects `/bin/true` and other stub executables before Slurm submission and validates SHUD binary visibility, shared libraries, project inputs, and generated forcing files.
- [ ] 5.2 Establish and document the node-22 Slurm path: gateway or host service health, allowed sbatch template, log root, account/partition/resource policy, and accounting availability.
- [ ] 5.3 Persist Slurm submit/accounting receipts in pipeline job/event state, including job id, array task id when applicable, status, exit code, log URI, elapsed time, and resource metrics where available.
- [ ] 5.4 Add tests for stub executable rejection before submit, missing shared libraries, invalid gateway/self-reference, Slurm unavailable blocker, submit receipt persistence, active-job duplicate prevention, and failed/missing SHUD outputs.
- [ ] 5.5 Verify with `uv run pytest -q tests/test_shud_runtime.py tests/test_production_slurm_validation.py tests/test_slurm_array_contract.py tests/test_production_scheduler.py`, `uv run ruff check .`, and opt-in live Slurm evidence that reports PASS or BLOCKED.

## 6. Parse, Publish, and Display Boundary

- [ ] 6.1 Parse real SHUD outputs into `hydro.hydro_run` and `hydro.river_timeseries` with stable run/model/source/cycle/forcing identities and segment/time/unit counts.
- [ ] 6.2 Publish q_down display products, manifests, and bounded logs under `/ghdc/data/nwm/published` or the configured published root with supported `published://` or allowlisted URIs.
- [ ] 6.3 Keep frequency/flood publication readiness separate from q_down parsed display readiness; missing return-period curves or warning thresholds must become explicit unavailable/quality metadata, not fabricated readiness.
- [ ] 6.4 Persist pipeline stage/job/event records for download, convert, forcing, forecast, parse, q_down publish, and frequency/flood publish so `/ops` can read formal state rather than diagnostic script JSON.
- [ ] 6.5 Add tests for parse success, parse mapping failure, duplicate terminal hydro-run prevention, q_down publish success, frequency unavailable state, private workspace URI rejection, strict product identity, and incomplete-stage aggregate status.
- [ ] 6.6 Verify with `uv run pytest -q tests/test_output_parser.py tests/test_tile_publisher.py tests/test_orchestration_chain.py tests/test_monitoring_api.py`, `uv run ruff check .`, and published artifact/log URI evidence.

## 7. Node-22 E2E and Documentation

- [ ] 7.1 Add a node-22 E2E command/test that runs or blocks truthfully through download, canonical conversion, forcing, SHUD Slurm execution, parse, publish, DB counts, pipeline evidence, and artifact/log URI checks.
- [ ] 7.2 Add CI-safe deterministic tests for scheduler preflight, no-mutation dry-run, mocked source/gateway state transitions, artifact-root placement, and no false live readiness claim.
- [ ] 7.3 Update the two-node deployment/runbook docs to distinguish no-flag scheduler-once business validation from explicit `--workspace-root` diagnostic compatibility commands.
- [ ] 7.4 Fix runbook/table references so DB state uses `ops.pipeline_job` and `ops.pipeline_event`, while API payload field names are labeled separately.
- [ ] 7.5 Update the two-node deployment/runbook docs to state that node 22 owns automation and node 27 reads readonly DB plus `/ghdc/data/nwm/published` artifacts only.
- [ ] 7.6 Verify with `openspec validate m23-qhh-22-production-automation --strict --no-interactive`, focused backend tests, docs/static checks if present, and an opt-in node-22 live E2E command that reports PASS or BLOCKED with evidence under `artifacts/` or `/scratch/frd_muziyao`.
