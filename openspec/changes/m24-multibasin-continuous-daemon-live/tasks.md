# Tasks: m24 Multi-basin Continuous Daemon (Live)

Each numbered section maps to one GitHub issue. Drive via `dual-end-issue-workflow` (node-22 is the
test oracle; CI is the merge gate). This change is a **live-closure / proof delta** on m20/m23 — see
Dependencies.

## Receipt schema (shared by §0/§1/§2/§3/§4)

Each section writes `artifacts/m24/<run_id>/<section>.json` (section ∈
`baseline|gateway|warm_start|concurrency|multibasin|daemon`). A `validate_receipt` helper +
pytest enforce the contract; each issue's Verification runs it. Field contract (required unless
marked nullable):

- `schema_version` (str), `contract_id` (str), `run_id` (str), `node` (str), `command` (str),
  `timestamp` (ISO-8601 str), `status` (enum `PASS|BLOCKED`),
  `execution_mode` (enum `live_proof|deterministic`), `live_proof_accepted` (bool),
  `dependency_blocker` (str, nullable), `redaction` (obj `{db_dsn_redacted:bool, bounds:obj}`),
  `artifact_refs` (list of `{kind,uri}`),
  `identity` (obj `{source,cycle_time,model_id,basin_id,basin_version_id,river_network_version_id}`),
  `stages` (list of `{stage,status,counts:obj}`),
  `slurm` (obj `{job_id,array_task_id(nullable),original_task_id(nullable),accounting,log_uri}`),
  `published_uri` (str, nullable), `warm_start_quality`
  (enum `fresh|degraded_stale_init_state|cold_start_no_state|cold_start_stale_state`, nullable).
- A `BLOCKED` receipt MUST set `dependency_blocker` and MUST NOT set `live_proof_accepted=true`.

## 0. Pre-change baseline and evidence

- [x] 0.1 `openspec validate m24-multibasin-continuous-daemon-live --strict --no-interactive`.
- [x] 0.2 Emit `artifacts/m24/<run_id>/baseline.json` (schema above): node, redacted DB identity,
  cycle filters, active model counts, `hydro_run` status counts for GFS/IFS 2026060400, gateway
  `/api/v1/slurm/health` result, `hydro.state_snapshot` count (expected 0), and the claim that live
  QHH ran via `run_qhh_cycle.sh` while the generic scheduler has never run live (m20 0/33).

Evidence Floor: openspec valid; `baseline.json` present with all fields from one environment/time.

## P. Dependency gate: m23 #255 fresh forecast-cycle ingestion closure

This is a **dependency gate / tracking issue** (label `dependency-gate`), not an m24 implementation
issue. It carries no m24 code; it only gates §4.

- [x] P.1 Close (or record explicit `BLOCKED`) m23 Task 3.1–3.4 fresh GFS/IFS cycle ingestion in
  its own issue. m24 §4 consumes this capability and does **not** implement ingestion.

Evidence Floor: a closed/BLOCKED receipt for m23 #255; m24 §4 may not close while this is open.

> Gate decision (issue #287), 2026-06-04 — **OPEN (PASS, not BLOCKED)**. Evidence: m23 #255 CLOSED
> 2026-06-03; m23 tasks.md 3.1–3.4 reconciled/ticked; node-22
> `tests/test_production_scheduler.py tests/test_orchestration_chain.py` **564 passed** (HEAD
> 9f49cc7); live ingestion proven by m24 baseline.json (GFS+IFS `2026060400` each `frequency_done`).
> §4 daemon live proof may now consume fresh ingestion; this gate no longer blocks §4.

## 1. Slurm gateway deployment + live receipts on node-22

- [ ] 1.1 Implement/prove a standalone gateway app + systemd unit listening at
  `SLURM_GATEWAY_URL=http://127.0.0.1:8081`, serving `/api/v1/slurm/health`
  (`NHMS_SERVICE_ROLE=slurm_gateway` currently reserved/fail-fast).
- [ ] 1.2 Make `health()` probe `sbatch`/`squeue`/`sacct`/`scancel` (not only `sinfo --version`);
  scheduler preflight HTTP-probes the configured URL → typed pre-mutation blocker when unhealthy.
- [ ] 1.3 Prove mock-vs-real parity (submit→poll→terminal + template selection) gating live use.
- [ ] 1.4 Emit a short-job terminal receipt (submit→poll→terminal, log root under workspace) and a
  separate long-job cancel receipt (submit→cancel-while-active→cancelled/accounting); do not
  conflate terminal-poll and cancel.
- [ ] 1.5 Stale-job reconcile reads job ids from durable `pipeline_job`/pre-execution evidence (not
  gateway in-memory `_jobs`) and verifies candidate identity via `sacct`.
- [ ] 1.6 Confirm the production scheduler routes submission through the gateway; demote the
  diagnostic direct-`sbatch` runner.

Evidence Floor: gateway `/api/v1/slurm/health` receipt probing 4 binaries; parity test PASS;
short-job terminal receipt + long-job cancel receipt; restart reconcile-by-identity proof.
Verification: `uv run pytest -q tests/test_real_slurm_gateway.py tests/test_production_slurm_validation.py tests/test_slurm_array_contract.py` + `uv run ruff check .` + node-22 gateway proof receipts or BLOCKED.

## 2. Cross-cycle warm-start closure (analysis-segment + cohort wiring)

- [ ] 2.1 Implement path (b): a short analysis/nowcast segment `[T_N, T_{N+1}]` with
  `end_time == T_{N+1}`, setting `Update_IC_STEP` to a cadence that lands on `T_{N+1}` (default
  1440min misses 6h/12h cycles), and using a causal no-future-leak `[T_N,T_{N+1}]` forcing policy
  (cycle N `0..Δ` lead or best-available nowcast; ERA5 only in delayed-reanalysis mode), so
  `state_save_qc` persists the `T_{N+1}` end state as the next cycle's IC. (Path (a) restart-cadence
  only if timestamped non-overwriting restart artifacts are added later.)
- [ ] 2.2 Normalize the run state artifact (`*.cfg.ic`/`*.cfg.ic.update`) to canonical
  `state.cfg.ic`, recording the original SHUD filename and target `valid_time`; on consume,
  materialize/rename the canonical state to `<project_name>.cfg.ic` that SHUD reads.
- [ ] 2.3 Close cohort wiring: `_candidate_basin_manifest` emits the selected `init_state_uri`/
  checksum so scheduler basin record, cycle-stage manifest, and forecast runtime manifest agree.
- [ ] 2.4 Extend `StateSnapshot`/selection with lineage (source/cycle/lead, model package version,
  checksum); reject incompatible lineage / over-`max_lead` with a stable rejection code.
- [ ] 2.5 Add state-variable QC (row counts vs mesh/river/lake; range/non-negative for
  canopy/snow/surface/unsat/GW/river-stage/lake-stage; restart water-balance delta threshold).
- [ ] 2.6 Record warm-start quality using the canonical enum (`fresh`/`degraded_stale_init_state`/
  `cold_start_no_state`/`cold_start_stale_state`); stop using `create_qhh_shud_manifest.py` in
  production.
- [ ] 2.7 Add deterministic tests (new):
  - `test_saved_state_valid_time_equals_next_cycle_init` (asserts `valid_time == T_{N+1}`, and
    snapshot/`.cfg.ic` header/run start three-way agreement).
  - `test_cycle_cohort_forecast_manifest_uses_prior_cycle_saved_state` (three manifest surfaces
    share URI/checksum).
  - lineage-reject (with code) and corrupt/failed-QC fallback cases.

Evidence Floor: the named tests PASS; lineage-reject code + QC + three-way time covered.
Verification: `uv run pytest -q tests/test_warm_start.py tests/test_orchestration_chain.py tests/test_e2e.py` + `uv run ruff check .` + node-22 two-cycle warm-start receipt (cycle 2 `ic_file_uri`==cycle 1 snapshot, quality recorded) or BLOCKED.

## 3A. Concurrent submit-and-return with durable reservation

- [ ] 3A.1 Implement concurrent multi-candidate submission with a two-phase protocol: inside the
  lock write a durable reservation/`pipeline_job`/idempotency key per candidate, atomically bind
  `slurm_job_id` on submit, queryable via `candidate_state` before lock release.
- [ ] 3A.2 Tests: delayed gateway submit, two overlapping passes, scheduler kill/restart, `sacct`
  reconcile, kill-after-submit-before-bind, submit-timeout-unknown-result — at most one
  `pipeline_job` per idempotency key enters submitted/running; unknown submit reconciled via Slurm
  job name/comment/idempotency metadata, never blindly re-submitted; receipt shows overlapping
  submits.
- [ ] 3A.3 De-scoping concurrency is NOT a valid close of this issue: it requires a separate change
  to proposal/spec/design; §3A closes only on delivered concurrency.

Evidence Floor: reservation/idempotency + crash-window test set PASS; overlapping-submit receipt
(scope-decision records do NOT satisfy this Evidence Floor).
Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py` + `uv run ruff check .`.

## 3B. Multi-basin live identity + partial-success proof

- [ ] 3B.1 Add or seed a second registered runnable model/basin (minimal fixture acceptable).
- [ ] 3B.2 Prove ≥2 basins in one live pass to published products, per-basin identity in evidence.
- [ ] 3B.3 Identity through array retry/reindex: `original_task_id` maps a reindexed task back to
  its basin/segment; same-name segments in different river networks are not merged.
- [ ] 3B.4 Per-basin partial-success isolation: A fails (forcing/forecast/parse/frequency/publish),
  B publishes; cycle aggregate reflects partial; B excludes A.

Evidence Floor: ≥2-basin live pass receipt; reindex identity test; partial-success isolation test.
Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py` + `uv run ruff check .` + node-22 multi-basin live receipt or BLOCKED.

## 4. Continuous daemon live operation

- [ ] 4.1 Wire the daemon entrypoint for node-22 (systemd timer and/or `run_continuous`) using the
  documented env contract (`NHMS_SCHEDULER_*`, `SLURM_GATEWAY_*`, `WORKSPACE_ROOT`, roots).
- [ ] 4.2 Lease heartbeat/renewal with a token/generation (`lease_token`, `heartbeat_seq`/mtime,
  owner pid-start/boot id); stale reclaim is compare-and-swap on that token after reconciling
  host/pid + candidate reservation + `sacct`/`squeue` (a contender must not unlink if the
  token/heartbeat changed). Prove on node-22 shared `/scratch` with two independent processes,
  TTL < pass duration, heartbeat crossing TTL without reclaim, no double-submit; record real fs type.
- [ ] 4.3 Safe enable/disable (no new submit after a bounded pass exits; evidence stays queryable).
- [ ] 4.4 Node-22 live daemon receipt proving the m20/m23 chain ran via the generic scheduler (not
  diagnostic scripts), binding identity/statuses/counts/gateway receipt/warm-start quality/URIs.

Evidence Floor: NFS two-process heartbeat lock proof; daemon pass receipt PASS/BLOCKED with full
schema; evidence it did not invoke QHH scripts. (Requires P closed.)
Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_monitoring_api.py` + `uv run ruff check .` + node-22 daemon live receipt or BLOCKED.

## 5. Diagnostic retirement and documentation

- [ ] 5.1 New guardrail test `test_production_scheduler_does_not_invoke_qhh_diagnostic_scripts`
  asserting the production scheduler/chain path references/invokes none of `run_qhh_cycle.sh`,
  `run_qhh_continuous.py`, `create_qhh_shud_manifest.py` (existing `tests/test_qhh_scripts_static.py`
  covers the scripts themselves, not this production-path assertion).
- [ ] 5.2 Label those scripts diagnostic-only in code headers + docs, with a concrete smoke command
  and minimal pass condition if retained.
- [ ] 5.3 Update `docs/runbooks/qhh-22-business-bringup.md` and `progress.md`: production = generic
  daemon; diagnostic lane is bring-up fallback.
- [ ] 5.4 Update `infra/env/*.example`, `infra/compose.compute.yml`, systemd/timer docs for the
  daemon + gateway contract.

Evidence Floor: guardrail test PASS; runbook/progress updated; env/compose/timer docs updated.
Verification: `uv run pytest -q tests/test_qhh_scripts_static.py tests/test_orchestration_chain.py tests/test_production_scheduler.py` + `uv run ruff check .` + `openspec validate m24-multibasin-continuous-daemon-live --strict --no-interactive` + docs diff.

## Dependencies (single model)

- **m23 #255 fresh ingestion** is a **hard prerequisite** (§P): closed or explicitly BLOCKED in its
  own issue before §4 daemon live proof closes. §4 consumes it; it does not implement it.
- **m23** Task 5.5 live Slurm / 6.6 live publish are subsumed by §1/§4 live receipts.
- **m20 (0/33)**: scheduler-orchestration, slurm-array-runner-integration, registered-basin-cycle-
  discovery, multibasin-state-idempotency — code exists; m24 issues provide live proof; m20 tasks
  are checked as that proof lands.
- **m2 analysis warm-start**: state_manager / `nhms-state` machinery is the basis for §2 path (b).
