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
- [x] 1.4 Fix `nhms-pipeline plan-production` so omitted `--workspace-root`, lock, evidence, temporary, object-store, and published-root paths use documented environment/config defaults instead of `.nhms-workspace` under the app directory.
- [x] 1.5 Update `infra/compose.compute.yml`, env examples, and systemd/timer docs so scheduler-once and continuous/timer modes can run with explicit roots, locks, service role, source/model filters, and evidence paths.
- [x] 1.6 Verify with `uv run pytest -q tests/test_production_scheduler.py tests/test_production_slurm_validation.py` plus `uv run ruff check .` and a Docker `nhms-pipeline plan-production --plan` smoke without `--workspace-root`.

### Issue #253 Evidence Floor

- `nhms-pipeline plan-production --plan` with documented env roots and no `--workspace-root`: resolves `WORKSPACE_ROOT`, object-store, published, lock, evidence, and temp/runtime roots from env/config; evidence contains redacted resolved roots and no app-local `.nhms-workspace`.
- Missing/invalid/unwritable workspace, object-store, published, lock, evidence, or temp root: returns a stable pre-mutation blocker or CLI/config error before download, Slurm, SHUD, hydro, met, parse, or publish mutation.
- Explicit safe `--workspace-root` diagnostic path: remains supported and keeps lock/evidence under that workspace.
- `infra/compose.compute.yml` `scheduler-once` and documented continuous/timer examples: run the business validation command without manual root flags and with explicit roots, locks, service role, source/model filters, interval/max-pass bounds, and evidence paths.
- The live E2E requirements in `specs/compute-scheduler-operationalization/spec.md` remain downstream Task 7 evidence for #260; #253 only needs a Docker scheduler no-flag smoke that reports PASS or BLOCKED for the scheduler entrypoint.
- Required commands:
  - `uv run pytest -q tests/test_production_scheduler.py tests/test_production_slurm_validation.py`
  - `uv run ruff check .`
  - `openspec validate m23-qhh-22-production-automation --strict --no-interactive`
  - Docker smoke: `docker exec nhms-compute-compute-api-1 uv run nhms-pipeline plan-production --plan`, or `BLOCKED` evidence if the live container is unavailable.

### Issue #252 Evidence Floor

- `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py`: identity/status/URI contract fixtures pass without live runtime mutation.
- `uv run ruff check .`: contract helper/schema changes are lint-clean.
- `openspec validate m23-qhh-22-production-automation --strict --no-interactive`: OpenSpec remains valid.
- Non-goal evidence: implementation must not add live forecast download, SHUD execution, Slurm submission, parse/publish mutation, or frontend behavior in #252.

## 2. QHH Model Bootstrap

- [x] 2.1 Add or harden an idempotent QHH bootstrap command that imports/publishes the processed Basins package, creates or activates `core.model_instance`, and records package/manifest identity.
- [x] 2.2 Ensure the active QHH model exposes scheduler-required `model_id`, `basin_id`, `basin_version_id`, `river_network_version_id`, `model_package_uri`, `shud_code_version`, and runnable `resource_profile` metadata.
- [x] 2.3 Extend station seeding to validate `qhh.tsd.forc`, enforce station count, populate `met.met_station` forcing-grid rows, and report created/updated/unchanged counts.
- [x] 2.4 Seed or validate QHH output river/segment identities required by the output parser and publisher.
- [x] 2.5 Add tests for missing package, station count mismatch, repeated bootstrap idempotency, active model discovery with no `not_shud_model`/`incomplete_model_metadata` exclusion, and duplicate active model rejection.
- [x] 2.6 Verify with focused bootstrap/registry tests, `uv run pytest -q tests/test_production_scheduler.py`, `uv run ruff check .`, and `nhms-pipeline plan-production --plan --model-id <qhh_model_id>` evidence.

### Issue #254 Evidence Floor

- Successful bootstrap fixture: valid processed QHH package with `qhh.tsd.forc` -> one active scheduler-ready model, station rows, output identities, package identity/manifest evidence, and created/updated/unchanged counts.
- Idempotency fixture: repeated bootstrap with the same package identity -> no duplicate active model, station, or output identity rows; unchanged or updated counts are explicit.
- Missing package/project file fixture -> typed blocker and no scheduler-ready active QHH model.
- Station-count mismatch or malformed `qhh.tsd.forc` fixture -> typed station-count blocker and no future-cycle `met.forcing_version` / `met.forcing_station_timeseries` rows.
- Unsafe package path fixtures: relative/traversal QHH project path, out-of-root package path, symlink leaf or symlink ancestor, and non-regular `qhh.tsd.forc` -> typed path/file blocker, no scheduler-ready active QHH model, no station/output/future-cycle writes.
- Bounded discovery fixtures: broad unrelated `NHMS_BASINS_ROOT`, max-depth/max-entry package discovery overflow, oversized `qhh.tsd.forc`, oversized manifest/checksum file, malformed JSON manifest, or digest mismatch -> typed discovery/input blocker with bounded read evidence, no scheduler-ready active QHH model, and no unbounded directory or file read.
- Evidence-write containment fixture: bootstrap report/evidence path outside the approved workspace/evidence root, existing regular-file evidence directory lane, or no-clobber collision -> stable evidence-path blocker or intentionally omitted evidence according to the command contract; no overwrite of unrelated files and no scheduler-ready active QHH model.
- Partial-write rollback fixture: model/package metadata creation succeeds but station seeding or output identity seeding fails -> stable partial-bootstrap blocker, QHH not marked scheduler-ready, no duplicate or partial station/output identity rows visible to scheduler/parser/publisher, and failure evidence written or intentionally omitted according to the public command contract.
- Duplicate active model fixture -> duplicate-active-model blocker and no downstream forecast/forcing/SHUD submission.
- Scheduler discovery fixture: `plan-production --plan --model-id <qhh_model_id>` after bootstrap includes QHH without `not_shud_model`, `not_runnable`, or `incomplete_model_metadata` exclusion.
- Non-goal evidence: #254 must not create dynamic forecast values, forcing versions/timeseries for future cycles, SHUD/Slurm jobs, hydro results, parser output, published display artifacts, or frontend behavior.
- Required commands:
  - `uv run pytest -q tests/test_production_scheduler.py`
  - focused bootstrap/registry tests introduced by the PR
  - `uv run ruff check .`
  - `openspec validate m23-qhh-22-production-automation --strict --no-interactive`
  - `uv run nhms-pipeline plan-production --plan --model-id <qhh_model_id>` evidence, or BLOCKED evidence if live package/DB is unavailable.

## 3. Fresh Forecast Ingestion

- [ ] 3.1 Harden GFS/IFS cycle discovery with source-specific lag/lookback/horizon policy, 403/unavailable handling, operator filters, and typed blocked evidence.
- [ ] 3.2 Ensure download and canonical conversion require source-specific complete QHH forcing variable ids and per-valid-time lead coverage before marking canonical products ready.
- [ ] 3.3 Add idempotency and retry tests for completed canonical reuse, transient download failure, source unavailable, incomplete variables, source-specific horizon handling, and reduced-scope source filters.
- [ ] 3.4 Verify with focused adapter/canonical tests, `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py`, `uv run ruff check .`, and a dry-run evidence sample that cannot proceed to forcing when canonical coverage is incomplete.

### Issue #255 Evidence Floor

- GFS complete fixture: available source/cycle with `prcp_rate_or_amount`, `air_temperature_2m`, `relative_humidity_2m`, `wind_u_10m`, `wind_v_10m`, `pressure_surface`, and `shortwave_down` plus complete per-valid-time lead coverage -> canonical-ready evidence records `source`, `cycle_time`, policy identity, accepted horizon, variable set, per-valid-time lead counts, status, object/cache identity, and no false reduced-scope marker.
- IFS complete or shorter-horizon fixture: available source/cycle with `prcp_rate_or_amount`, `air_temperature_2m`, `relative_humidity_2m`, `wind_u_10m`, `wind_v_10m`, `surface_pressure`, and `shortwave_down` over the configured IFS horizon -> canonical-ready or reduced-scope evidence records accepted horizon and policy identity without applying the GFS pressure variable contract.
- Missing variable fixture -> canonical blocked/incomplete evidence lists safe missing variable ids, keeps `met.canonical_met_product` or scheduler query out of canonical-ready state, and submits no forcing/SHUD candidate.
- Missing lead/valid-time fixture -> canonical blocked/incomplete evidence lists safe missing lead/valid-time counts, keeps downstream forcing/SHUD unsubmitted, and does not fabricate full-scope readiness.
- Provider unavailable/forbidden/stale/policy-filter fixture -> evidence records typed `unavailable`, `forbidden`, `stale`, `unsupported`, or `policy_blocked` classifier with source/cycle/probe identity and no canonical-ready product.
- Provider secret-redaction fixture: credential-bearing URL/header/env mock or signed URL returning 403/forbidden -> evidence records only safe typed reason and source/cycle/probe identity; it must not include tokens, signed URL query strings, authorization headers, credential env var names, or credential values.
- Transient download failure fixture -> retryable evidence records attempt count and next eligible retry behavior, does not submit forcing/SHUD, and does not turn deterministic/mock fallback into live business readiness.
- Operator filter fixture: source/model/basin/lookback/lag/max-cycle filters -> scheduler evidence records filters and distinguishes reduced-scope runs from full default automation.
- Cache/object/path safety fixtures: out-of-root cache path, traversal object reference, existing canonical object collision, and partial download artifact -> typed blocker or quarantined incomplete state; no overwrite of ready canonical products and no write outside approved cache/evidence roots.
- Persistence/idempotency fixture: repeated scan for the same `source + cycle_time + object identity + policy identity` -> reuse the existing completed `met.forecast_cycle` / `met.canonical_met_product` readiness without duplicate ready rows; changed source object or policy identity must not reuse stale readiness.
- Downstream query fixture: incomplete, unavailable, retryable, policy-blocked, or reduced-scope-disallowed state -> production scheduler/forcing query cannot treat the product as canonical-ready and submits no forcing, SHUD, parse, or publish work.
- Release/dependency fixture: default CI and focused mock adapter/canonical tests do not require live ECMWF/CDS credentials, real provider access, or optional unavailable GRIB/provider libraries; missing optional provider dependency yields skip/BLOCKED evidence or typed unavailable state rather than import-time failure.
- Non-goal evidence: #255 must not create station forcing interpolation output, `met.forcing_version`, `met.forcing_station_timeseries`, SHUD/Slurm jobs, hydro results, parser output, published display artifacts, or frontend behavior.
- Required commands:
  - focused adapter/canonical tests introduced by the PR
  - `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py`
  - `uv run ruff check .`
  - `openspec validate m23-qhh-22-production-automation --strict --no-interactive`
  - dry-run scheduler evidence sample showing PASS only for complete canonical coverage or BLOCKED/UNAVAILABLE with exact dependency when coverage/provider state is incomplete.

## 4. Fixed-Station Forcing Production

- [x] 4.1 Scheduler passes model/basin/network and canonical readiness identity into forcing production; producer validates repository identity.
- [x] 4.2 Forcing versions persist canonical lineage, station/timestep/variable counts, units, time range, manifest checksum, and child-row completeness proof.
- [x] 4.3 SHUD forcing packages include safe station filenames, contiguous station order, package URI, checksums, units, time range, and source/cycle identity.
- [x] 4.4 Tests cover missing/corrupt children, identity mismatch, fixed-station contract, resource bounds, ERA5 `mm/day`, idempotency, package generation, and no rSHUD runtime dependency.
- [x] 4.5 Verified with required pytest suite, `uv run ruff check .`, and forcing evidence fields for station count, variable count, time range, units, package URI, and manifest checksum.

### Issue #256 Evidence Floor

- Complete forcing fixture: active QHH model plus fixed `forcing_grid` stations and complete canonical products -> one ready `met.forcing_version`, complete `met.forcing_station_timeseries` rows for every station/variable/valid time, `met.forcing_version_component` lineage, station count, variable set, units, valid time range, package URI, and manifest checksum.
- Scheduler glue fixture: a scheduler/orchestration candidate with #255 canonical-ready evidence invokes forcing generation using the active QHH model/basin/source/cycle/canonical identity and records forcing-stage evidence; blocked/incomplete canonical candidates create no forcing rows or package files.
- Missing station fixture: no active `forcing_grid` stations, missing SHUD forcing index, missing forcing filename, or station count mismatch -> typed missing-stations blocker, no ready forcing version, no station timeseries ready state, and no SHUD runtime submission.
- Coverage/quality fixture: missing canonical value for a station/variable/time, non-finite value, unit mismatch, or reduced-scope coverage not permitted -> typed interpolation/coverage/quality blocker with safe station/variable/time evidence and no ready forcing version.
- Idempotency fixture: repeated generation for identical model/source/cycle/canonical/station/grid identity -> no duplicate ready forcing versions, deterministic replacement/reuse behavior, stable checksum, and no duplicated station timeseries rows.
- Stale identity fixture: changed canonical product/checksum/lineage, changed station set, changed grid definition, or changed forcing window under the same model/source/cycle -> stale forcing reuse is rejected and replacement or blocked evidence is recorded according to policy.
- Partial-write rollback fixture: parent `met.forcing_version` creation succeeds but component/timeseries/package/manifest write fails -> parent remains incomplete/non-ready, child rows/files are absent or safely replaceable, and retry finalizes without duplicate ready versions.
- SHUD package fixture: generated runtime package includes `qhh.tsd.forc` plus per-station forcing files with bootstrap station ordering, filenames, checksums, units, source/cycle identity, and time range in the runtime manifest.
- rSHUD non-runtime fixture: forcing file generation follows the processed basin file contract but does not import/call rSHUD or AutoSHUD as a runtime solver/data generator.
- Path/evidence fixture: package and manifest writes stay under approved workspace/object-store roots, reject traversal/out-of-root paths, and do not leak private compute paths as display-ready published artifacts.
- Non-goal evidence: #256 must not execute SHUD, submit Slurm jobs, create `hydro.hydro_run` or `hydro.river_timeseries`, parse q_down output, publish display artifacts, or change frontend behavior.
- Required commands:
  - `uv run pytest -q tests/test_forcing_producer.py tests/test_orchestration_chain.py tests/test_production_scheduler.py`
  - `uv run ruff check .`
  - `openspec validate m23-qhh-22-production-automation --strict --no-interactive`
  - forcing evidence sample containing station count, variable count, valid time range, units, package URI, and manifest checksum, or BLOCKED evidence with exact missing dependency.

## 5. Real SHUD and Slurm Execution

- [x] 5.1 Add scheduler/orchestrator pre-submit runtime preflight that rejects `/bin/true` and other stub executables before Slurm submission and validates SHUD binary visibility, shared libraries, project inputs, and generated forcing files.
- [x] 5.2 Establish and document the node-22 Slurm path: gateway or host service health, allowed sbatch template, log root, account/partition/resource policy, and accounting availability.
- [x] 5.3 Persist Slurm submit/accounting receipts in pipeline job/event state, including job id, array task id when applicable, status, exit code, log URI, elapsed time, and resource metrics where available.
- [x] 5.4 Add tests for stub executable rejection before submit, missing shared libraries, invalid gateway/self-reference, Slurm unavailable blocker, submit receipt persistence, active-job duplicate prevention, and failed/missing SHUD outputs.
- [ ] 5.5 Verify with `uv run pytest -q tests/test_shud_runtime.py tests/test_production_slurm_validation.py tests/test_slurm_array_contract.py tests/test_production_scheduler.py`, `uv run ruff check .`, and opt-in live Slurm evidence that reports PASS or BLOCKED.

## 6. Parse, Publish, and Display Boundary

- [x] 6.1 Parse real SHUD outputs into `hydro.hydro_run` and `hydro.river_timeseries` with stable run/model/source/cycle/forcing identities and segment/time/unit counts. (existing `workers/output_parser`, consumed by #259)
- [x] 6.2 Publish q_down display products, manifests, and bounded logs under `/ghdc/data/nwm/published` or the configured published root with supported `published://` or allowlisted URIs. (`TilePublisher.publish_qdown_cycle`; live publish-tiles wiring → #260)
- [x] 6.3 Keep frequency/flood publication readiness separate from q_down parsed display readiness; missing return-period curves or warning thresholds must become explicit unavailable/quality metadata, not fabricated readiness.
- [x] 6.4 Persist pipeline stage/job/event records for download, convert, forcing, forecast, parse, q_down publish, and frequency/flood publish so `/ops` can read formal state rather than diagnostic script JSON. (chain `M3_STAGES` publish stage covers q_down-publish persistence)
- [x] 6.5 Add tests for parse success, parse mapping failure, duplicate terminal hydro-run prevention, q_down publish success, frequency unavailable state, private workspace URI rejection, strict product identity, and incomplete-stage aggregate status. (`test_output_parser` + `test_tile_publisher` + chain partial-status tests)
- [ ] 6.6 Verify with `uv run pytest -q tests/test_output_parser.py tests/test_tile_publisher.py tests/test_orchestration_chain.py tests/test_monitoring_api.py`, `uv run ruff check .`, and published artifact/log URI evidence.

## 7. Node-22 E2E and Documentation

- [x] 7.1 Add a node-22 E2E command/test that runs or blocks truthfully through download, canonical conversion, forcing, SHUD Slurm execution, parse, publish, DB counts, pipeline evidence, and artifact/log URI checks. (existing `e2e_validation.py` 9-stage harness + `test_two_node_22_e2e.py` live opt-in; publish stage exposed via new `publish-qdown` CLI entrypoint)
- [x] 7.2 Add CI-safe deterministic tests for scheduler preflight, no-mutation dry-run, mocked source/gateway state transitions, artifact-root placement, and no false live readiness claim. (preflight/dry-run/storage existing; mocked gateway state transitions + no-false-readiness added in `test_cli_publish_qdown.py`)
- [x] 7.3 Update the two-node deployment/runbook docs to distinguish no-flag scheduler-once business validation from explicit `--workspace-root` diagnostic compatibility commands.
- [x] 7.4 Fix runbook/table references so DB state uses `ops.pipeline_job` and `ops.pipeline_event`, while API payload field names are labeled separately.
- [x] 7.5 Update the two-node deployment/runbook docs to state that node 22 owns automation and node 27 reads readonly DB plus `/ghdc/data/nwm/published` artifacts only.
- [ ] 7.6 Verify with `openspec validate m23-qhh-22-production-automation --strict --no-interactive`, focused backend tests, docs/static checks if present, and an opt-in node-22 live E2E command that reports PASS or BLOCKED with evidence under `artifacts/` or `/scratch/frd_muziyao`.
