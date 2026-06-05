# Issue #292 — M24-4 Continuous daemon live operation (worklog)

Branch: `feat/issue-292-continuous-daemon`. Node-22 receipts under
`artifacts/m24/m24-daemon-5880d09/` (gitignored; paths cited below).

## Scope outcome

| §   | Item | Status |
|-----|------|--------|
| 4.1 | Daemon entrypoint + documented env contract | **DONE (pre-existing, m23)** — generic scheduler `nhms-pipeline plan-production --continuous --max-passes N` (`services/orchestrator/cli.py`) → `run_continuous`; systemd timer companion units + env contract (`NHMS_SCHEDULER_*`, `SLURM_GATEWAY_*`, roots) documented in `infra/systemd/nhms-compute-compose.service`. |
| 4.2 | Lease heartbeat/renewal + CAS reclaim + owner-liveness reconcile | **DONE (code + live NFS proof)** |
| 4.3 | Safe enable/disable (delta on m23 contract) | **DONE (pre-existing, m23)** — `systemctl disable --now nhms-compute-scheduler.timer` + `reset-failed`; manual `scheduler-once` proof path preserved. |
| 4.4 | Node-22 live daemon receipt (m20/m23 chain via generic scheduler) | **DONE — daemon-mode generic execution proven live; submission BLOCKED on upstream m23 canonical ingestion (exact dependency); end-to-end completion corroborated by #291** |
| 4.5 | GRIB-env preflight fail-loud on daemon startup | **DONE (code + live proof)** |

## Code deliverables

### §4.5 — GRIB-env preflight fail-loud (commit aa4aa46)
`services/orchestrator/scheduler.py` `_slurm_grib_env_check(config, *, probe=None)`, wired into
`_slurm_preflight` (the per-submission fail-loud gate, same as gateway/shud blockers). Blocks before
any sbatch when the compute node would lack GRIB libs:
- `NHMS_GRIB_ENV_ROOT` set but `<root>/bin|lib` missing → `GRIB_ENV_ROOT_INVALID`.
- root unset + system eccodes not asserted (injectable probe; fail-safe BLOCKED on probe error) →
  `GRIB_ENV_UNAVAILABLE`.
Only a valid root or an explicit `NHMS_GRIB_SYSTEM_ECCODES` assertion passes → the #291-class silent
GRIB skip is impossible. 7 deterministic tests.

### §4.2 — Lease heartbeat + CAS reclaim (commit 5880d09)
`FileSchedulerLease.renew()` (CAS on `pass_id`+`lease_token`, rewrites with `heartbeat_seq+1` +
`os.utime` to refresh mtime), `_LeaseHeartbeat` daemon thread (renew every `max(1, ttl//3)`s, started
after acquire / stopped in `finally` before release), owner-liveness reconcile in the stale decision
(same-host pid alive → never reclaim; provably dead → reclaim; cross-host unknown → require 2×TTL
silence; injectable `owner_liveness_probe`), and CAS-on-reclaim (re-read under the guard before
unlink; a holder that renewed is never unlinked). Closes the double-submit hole where a pass longer
than `lock_ttl_seconds` aged the lock mtime past TTL and a second daemon instance reclaimed a live
lease. 9 deterministic tests.

## Live receipts (node-22, host `xnode`, `/scratch` = **nfs**)

### §4.2 lease NFS two-process proof — `artifacts/m24/m24-daemon-5880d09/lease_nfs.json` → **PASS**
`scripts/m24_lease_nfs_proof.py`, two independent fork processes on the real NFS lock, TTL=2s:
- **P1 (no double-submit):** holder A acquires + heartbeats across 6s (3×TTL, `heartbeat_seq`
  reached 8); contender B's 10 acquire attempts during the long pass → **0 acquired** (a live holder
  is never reclaimed even past TTL).
- **P2 (no deadlock):** a holder C that dies (pid gone) without releasing is reclaimed by B after
  TTL (owner-liveness reconcile).

### §4.5 GRIB preflight proof — `artifacts/m24/m24-daemon-5880d09/grib_preflight.json` → **PASS**
`scripts/m24_grib_preflight_proof.py`: root unset → `GRIB_ENV_UNAVAILABLE` (loud); real root
`/scratch/frd_muziyao/nhms-grib` (bin+lib present) → no blocker; non-existent root →
`GRIB_ENV_ROOT_INVALID` (loud).

### §4.4 daemon-mode generic-execution receipt — `artifacts/m24/m24-daemon-5880d09/daemon_pass.json`
Command (NOT the diagnostic `scripts/run_qhh_continuous.py`):
`nhms-pipeline plan-production --continuous --max-passes 1 --submit --source gfs --model-id
basins_qhh_shud --basin-id basins_qhh` (env sourced from `infra/env/compute.host.env`).
- `execution_mode = production_orchestration` (real `--submit`, not dry), `lock.acquired = true`
  (daemon lease carries `lease`), `root_preflight.status = ready`, **no GRIB blocker** (§4.5 live).
- Candidate `gfs_2026060518` / `basins_qhh` discovered with the FULL production identity contract
  (`complete=true`, run_id `fcst_gfs_2026060518_basins_qhh_shud`, `output_segment_count=1633` — the
  #291 `.sp.riv` fix), gateway healthy (`slurm-wlm 23.11.4`, sbatch/squeue/sacct/scancel).
- `submitted_count = 0`: the scheduler correctly **refused to submit**, blocked on
  `canonical_identity_mismatch` — the fresh cycle's canonical (`canon_gfs_2026060518`) is not yet
  ingested (401 canonical variables `missing`). Per the m24 **P gate**, §4 *consumes* m23 fresh
  ingestion and does not implement it; an unmet upstream ingestion dependency is the spec's BLOCKED
  scenario, recorded with the exact dependency.

**Corroboration:** #291 proved the SAME generic scheduler (`run_once`/`run_continuous`) driving the
full m20/m23 chain to PUBLISHED end-to-end for `gfs_2026060500` (both basins, per-basin identity,
gateway receipts, warm-start quality, manifest/log URIs). §4.4's end-to-end completion is therefore
proven when the upstream canonical is ready; this pass proves daemon-mode generic execution + the
fail-loud upstream gate when it is not.

## CI merge-gate fixes (bundled per user request, commit 20b8654)
Master CI was left red by #290/#291. Fixed on this branch:
- publisher degrade catches `DELIVERY_SCHEMA_MISSING` (qdown `river_timeseries` absent) → honest
  `NO_PUBLISHABLE_PRODUCTS` carrying flood residual_blockers (3 `test_slurm_array_contract` tests).
- `test_analysis_pipeline` CapturingGateway matches the real `_submit_rendered_script` signature
  (#290 keyword-only `comment=`).
- `test_worker_chain_smoke` replaces the stale `STATE_TIME` sentinel with a structural warm-start
  `.cfg.ic` check (mock writes a real numeric IC).
- `qhh-22` runbook 4 prose lines re-wrapped under MD013 (markdownlint-cli2 → 0 errors).

## Verification
`uv run pytest -q tests/test_production_scheduler.py tests/test_monitoring_api.py` (scheduler lease +
GRIB + monitoring) and `uv run ruff check .` clean (modulo the machine-local `/tmp`-symlink env issue
that fails unrelated object-store tests identically on base). Node-22 live receipts PASS as above.
