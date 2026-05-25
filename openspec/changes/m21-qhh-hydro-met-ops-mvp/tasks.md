## 0. Change Preflight and QHH Baseline

- [ ] 0.1 Validate this OpenSpec change with `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive` before implementation issues begin.
- [ ] 0.2 Freeze the QHH baseline for the implementation pass: latest real run, `forcing_version_id`, station count, segment count, source/cycle coverage, and known IFS horizon limits.
- [ ] 0.3 Record in the Epic that forcing producer already writes `met.forcing_station_timeseries`; this phase validates readiness, indexes, APIs, and UI consumption rather than rebuilding forcing writes.
- [ ] 0.4 Record in the Epic that formal MVP operations use `nhms-pipeline plan-production` and pipeline/orchestrator persistence, while qhh scripts remain diagnostic/reproduction tools.

## 1. Backend Station Series and Forcing Readiness

- [ ] 1.1 Implement `packages/common/forecast_store.py` station series query support over `met.forcing_station_timeseries`, including forcing version, model/source/cycle resolution, variable/time filters, limit, truncation, units, and quality flags.
- [ ] 1.2 Implement `GET /api/v1/met/stations/{station_id}/series` in the data-source API route with validation, not-found/unavailable errors, request metadata, and no synthetic samples.
- [ ] 1.3 Update `openapi/nhms.v1.yaml`, regenerate frontend API types, and remove the station series route from any deferred OpenAPI drift allowlist.
- [ ] 1.4 Add backend tests for explicit `forcing_version_id`, `model_id + source_id + cycle_time` resolution, variable filtering, time filtering, limit/truncated behavior, missing station, missing forcing version, and invalid parameters.
- [ ] 1.5 Add QHH forcing readiness checks or tests that confirm selected existing QHH forcing versions expose expected/actual station counts near the 386 seeded stations, six MVP station variables, units, quality flags, missing-data reasons, and any required query indexes.

## 2. Latest Product Discovery

- [ ] 2.1 Implement a stable latest QHH product API or equivalent aggregation returning model, basin version, river network version, source, cycle, run, forcing version, station count, segment count, status, and availability metadata.
- [ ] 2.2 Add selection rules that reject failed, cancelled, incomplete, or identity-missing products and report unavailable reasons when no usable product exists.
- [ ] 2.3 Add tests for latest GFS/IFS selection, incomplete-product rejection, source/cycle normalization, station/segment counts, and IFS shorter-horizon metadata.
- [ ] 2.4 Update OpenAPI and frontend generated types for the latest-product contract.

## 3. Hydro-met MVP Frontend

- [ ] 3.1 Add `/hydro-met` route or route alias and MVP navigation entry while preserving existing `/meteorology`, `/forecast`, `/segments/:segmentId`, and `/monitoring` routes.
- [ ] 3.2 Build the hydro-met bootstrap data adapter using latest-product metadata, station inventory, river segment list, station series, and forecast-series APIs without manual IDs.
- [ ] 3.3 Implement station selection UI for map/list/detail and six-variable forcing charts with source, cycle, forcing version, valid-time range, unit, quality flag, truncation, and unavailable states.
- [ ] 3.4 Implement river segment selection UI for real `q_down` forecast-series curves, GFS/IFS source selection, unit metadata, empty-state handling, and IFS shorter-horizon labels.
- [ ] 3.5 Replace MVP-facing "water level/stage" wording with river discharge or river-segment flow wording wherever it describes `q_down`.
- [ ] 3.6 Add frontend unit/component tests and browser smoke coverage for `/hydro-met` bootstrap, station selection, river selection, unavailable states, and no-synthetic-data behavior.

## 4. Ops Backend Pipeline Control

- [ ] 4.1 Ensure QHH formal scheduler/orchestrator execution writes or exposes canonical `download`, `convert`, `forcing`, `forecast`, `parse`, `frequency`, and `publish` stage records with run id, status, Slurm job id, timestamps, retry count, and bounded `log_uri`.
- [ ] 4.2 Ensure stage and jobs data comes from formal pipeline/orchestrator APIs and persisted job records, not qhh diagnostic state JSON files.
- [ ] 4.3 Add backend tests for stage mapping, source/cycle filtering, `run_id`, Slurm job id, timestamps, duration, `retry_count`, bounded `log_uri`, failed status handling, and retry metadata updates.
- [ ] 4.4 Confirm OpenAPI/frontend types either already expose the required ops fields or update them in the same PR with drift checks.

## 5. Ops MVP Frontend

- [ ] 5.1 Add `/ops` route or route alias and MVP navigation entry over the monitoring workflow with operator RBAC behavior preserved.
- [ ] 5.2 Converge the ops page to source/cycle selector, stage cards, stage progress, jobs table, log modal, retry action, queue depth, and compact success/duration metrics.
- [ ] 5.3 Wire failed-run restart buttons for `failed`, `submission_failed`, `partially_failed`, and `permanently_failed` states to `POST /api/v1/runs/{run_id}/retry`, refresh status after retry, and preserve authorization gating for mutating actions.
- [ ] 5.4 Add frontend tests for `/ops` stage display, jobs table fields, log modal success/error, retry button visibility, retry API call, non-operator gating, and source/cycle filtering without mixed-cycle jobs.

## 6. QHH Smoke, Evidence, and Documentation

- [ ] 6.1 Add or update an MVP smoke command/runbook that validates one accepted QHH GFS cycle from download through station series, forecast-series, `/hydro-met`, and `/ops`, or records the exact missing live dependency.
- [ ] 6.2 Add required IFS live or deterministic evidence for GFS/IFS parallel display and 06/18 UTC shorter-horizon labeling; skipped live IFS proof must record the exact missing dependency and must not claim IFS live readiness.
- [ ] 6.3 Add controlled failure/retry evidence showing failed stage visibility, retry job creation, Slurm/job metadata update, and terminal outcome.
- [ ] 6.4 Record validation commands for backend tests, OpenAPI drift, frontend API type check, frontend tests/build, browser smoke, and opt-in live smoke dependencies.
- [ ] 6.5 Update `progress.md`, MVP launch plan, and QHH runbooks so they reflect delivered MVP scope, formal scheduler boundary, qhh diagnostic-script boundary, accepted `no_frequency_curve` boundary, and explicit P2 exclusions.
