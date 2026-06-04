## Why

The QHH MVP is proven: GFS and IFS full chains both reached `frequency_done` on node-22
(download → canonical → forcing → SHUD → parse → frequency → publish). But that live run was
driven by `scripts/run_qhh_cycle.sh` + `scripts/run_qhh_continuous.py` — the diagnostic shell
lane that m23 `design.md` explicitly **rejected for production automation**. The generic
production orchestrator (`services/orchestrator/chain.py` + `scheduler.py`) — which m20 specified
for multi-basin continuous automation and which already implements candidate-cohort discovery,
`M3_STAGES`, Slurm array stages, and warm-start state handling — has **never run live on
node-22**. m20's 33 tasks are 0/33; the chain path is only deterministic-tested.

Three concrete gaps block "full continuous daemon, multi-basin, parallel":

1. The chain path submits **only** through `HttpSlurmGatewayClient` (POST to the Slurm HTTP
   gateway), and that gateway service is **not deployed on node-22** — the live runs bypassed it
   with direct `sbatch`.
2. Cross-cycle **warm start** (use cycle N-1's SHUD state at cycle N's init time as the initial
   condition) has a real time-semantics gap: `nhms-state save` keys the snapshot at the forecast
   `end_time` (forecast-window end, e.g. T+7d), not the next cycle's init time (e.g. T+12h). It is
   also not wired through the cohort path (the basin record omits `init_state_uri`) and has never
   been live-proven. The QHH live path used a fixed packaged calibrated state every cycle — no
   hydrologic memory across cycles.
3. Multi-basin **parallel** dispatch: within-cycle array fan-out exists (m20), but cross-candidate
   concurrent submit-and-return does **not** — cohorts run sequentially today (scheduler.py:1349).
   Concurrent dispatch (what the operator means by "并行发起任务") is net-new and needs a durable
   reservation protocol; ≥2-basin live identity/partial-success proof is also unproven.

This change operationalizes m20's generic scheduler as the live continuous daemon — multi-basin,
parallel, warm-started — and retires the diagnostic scripts as the production path.

## What Changes

- Deploy and health-check the Slurm HTTP gateway on node-22 (the generic chain already submits
  only via the gateway; it has never been deployed live here), prove real-vs-mock parity plus
  stale-job reconciliation, and demote the diagnostic direct-`sbatch` runner.
- Close cross-cycle warm-start chaining — including the time-semantics gap that today's snapshot is
  keyed at the forecast-window `end_time`, not the next cycle's init time: each completed cycle
  persists its SHUD state valid **at the next cycle's init time**; the next cycle consumes it as
  `initial_state.ic_file_uri` with lineage and freshness checks and cold-start fallback; the cohort
  `init_state_uri` propagation is wired end-to-end and live-proven for forecast→forecast continuity.
- Add concurrent multi-candidate dispatch (submit-and-return across independent basin/source/cycle
  candidates — net-new beyond m20's sequential cohort execution) and prove ≥2 basins run live in
  one pass with strict identity isolation and per-basin partial-success isolation.
- Run the production scheduler as a bounded/continuous daemon on node-22 (systemd timer or
  `run_continuous`) via the documented env contract, with lock/lease, safe enable/disable, and
  pass evidence — live-proven from fresh forecast cycle to published products.
- Formally demote `run_qhh_cycle.sh` / `run_qhh_continuous.py` to a diagnostic/runbook lane;
  production automation must not depend on QHH-specific shell scripts (m20 non-goal made
  enforceable), with docs/runbook updated.

## Capabilities

### New Capabilities

- `continuous-daemon-live-operation`
- `cross-cycle-warm-start-chaining`
- `multibasin-parallel-dispatch`
- `slurm-gateway-node22-deployment`
- `diagnostic-script-production-retirement`

## Impact

- `services/orchestrator/*` (scheduler, chain, persistence, retry), `services/slurm_gateway/*`,
  `infra/sbatch/*`, `infra/env/*`, `infra/compose.compute.yml`, systemd/timer units.
- `scripts/create_qhh_shud_manifest.py` (no longer the production manifest builder; chain's
  `_prepare_forecast_runtime_manifests` is authoritative), `scripts/run_qhh_cycle.sh` and
  `scripts/run_qhh_continuous.py` (demoted to diagnostic lane).
- `packages/common/state_manager.py` / `nhms-state` warm-start wiring; `workers/shud_runtime`
  IC staging.
- node-22 deployment: Slurm gateway service, runtime/lock/evidence roots, daemon unit.
- Builds on and drives toward closure of m20 capability specs; consumes m23 QHH bootstrap.

## Non-Goals

- Frontend UI for the daemon (ops surfaces remain read-only display per m22).
- Claiming national all-basin coverage; "multi-basin" here means ≥2 registered runnable models
  proving generality, starting from QHH plus one additional fixture/basin.
- CLDAS as a required source; ERA5 near-real-time ingest.
- Changing the scientific algorithms or business semantics of download/canonical/forcing/SHUD/
  parse/frequency. This change is orchestration + deployment + warm-start wiring; it MAY modify
  runtime wiring for warm-start IC staging, state-save, state QC, the analysis-segment control
  (`Update_IC_STEP`, causal forcing), and the manifest IC contract.
- Fabricating return periods, warning levels, station forcing, or weather data when unavailable.
- Re-running full live multi-cycle GFS/IFS chains inside fast CI.
