# Pre-forecast resume repair and dual-source recovery

Issue: #2439. PR: #2445. Independent follow-on: #2447.

## Scope and deployment

The strict warm-start manifest upgrader now preserves recognized unfinished
convert/forcing resumes. Forecast/later mismatch handling and the forcing
artifact guard remain strict. No circuit reset, manual journal edits, forged
forcing witness, or warm-to-cold fallback was used.

Reviewed feature source: `c9589e0fb75f4b4fa28b8890c613775dc514ecf5`.
Node-22 baseline: `7b38bcb8bebd65ce051dae0771c896e1ca96c0fc`.
Deployed descendant: `e7bd816060a25b72112048c9b24e9d8a9980581a`.
Only `services/orchestrator/scheduler_candidates.py` was backported;
source patch-id on both trees: `299d90a4fe444bc7cc39cc7521a423222d1e54ab`.
The mainline tree was not rolled onto production.

The user scheduler timer was paused at 13:55:49Z. Its old pass drained normally
at 14:02:47Z. Deployment admission checked no active scheduler readers or Slurm
jobs, clean tracked files and preserved `.nhms-work/`. Deployment completed at
14:04:37Z. Compute/gateway environment files and interpreter configuration were
unchanged; the active interpreter remained Python 3.12.7. No `uv sync` or
runtime environment replacement occurred. Node-22 remained DB-free.
Rollback to the prior tree requires another quiescent window; do not switch
source underneath running workers.

## Regression evidence

Node-27 isolated checkout, with the production environment reused only after
matching dependency manifests and with `TMPDIR=/home/nwm/tmp`:

- Test-only commit `f9558fc57dfd4cde053e9afa1c20653aa61b23ab`: captured
  convert-only decision chain failed because forcing became forecast.
- Patched source plus corrected consumer oracle at `c9589e0f`: six focused
  cases passed; `test_production_scheduler.py`, `test_forced_resubmit_veto.py`
  and `test_warm_start_chaining.py`: **2197 passed in 455.44s**.
- The first suite run's sole failure was an incorrect new assertion that an
  ordinary resume should not use retry-scoped execution. The unchanged
  consumer intentionally does use that execution scope; the test now checks
  this while still rejecting terminal-stage forced resubmission. The correction
  is explicit in `evidence/test-oracle-correction.txt`.
- Local Ruff, strict OpenSpec validation and the changed runbook Markdown check
  passed. These are not substitutes for the live recovery evidence below.

## Live recovery

First bounded pass: `scheduler_2026091614_9b8dfdb3c8d4`, started 14:05:02Z,
finished 15:43:53Z. GFS cycle `2026091412` completed all 38 tasks in each array:
forcing **49028**, forecast **49066**, state-save/QC **49117**; all Slurm exits
were `0:0`. The qhh successor state was
`state_gfs_dg_0883c7e9c1006c6fd347df500315e9df_2026091500_gfs_2026091412_f012`,
with a successful state-save witness. Node-27 received the run/output tree.
The scheduler timer was restored active/enabled at 15:47:08Z.

Node-27 user autopipe PID3339699 ran 15:51:40Z–16:13:22Z: ingested GFS38,
processed38, published38, failed0, skipped0, exit0. Its first published recovery
cycle was `2026-09-14T12Z`, newer than the prior GFS `2026-09-14T00Z`.

IFS initially hit an independent response failure: forcing array **49174**
was accepted and completed 38/38, but the gateway captured empty sbatch stdout
and returned HTTP502 `SLURM_PARSE_ERROR`. The old convert-cohort journal row
became permanently_failed with no Slurm binding. Preserved scontrol Comment,
accounting and all 38 forcing_ready stdout witnesses establish actual execution;
stdout alone is not object-store integrity proof. The physical cause of the
empty capture remains unproven.

The next normal timer pass `scheduler_2026091617_33080980c3c3` ran
17:43:58Z–19:04:50Z and selected the same IFS cycle through a forcing-cohort run
identity: forcing **49309**, forecast **49347**, state-save/QC **49385** all
succeeded. Thus IFS recovered through automatic repeated forcing execution,
**not** by adopting 49174. No operator retry/adoption was invoked. The earlier
claim that this row permanently blocked IFS publication was superseded by these
observations. The ambiguity/cross-cohort duplicate-submission defect remains
tracked in #2447, which the user authorized as separate follow-on implementation.
An adoption tool must refuse the now-superseded old attempt.

Pass `scheduler_2026091619_936c2814d474` completed at 21:28:31Z with 76 selected
models, 76 submitted, zero failed/partial/blocked. Its terminal stage evidence:

| Source/cycle | Convert | Forcing | Forecast | State-save/QC |
| --- | --- | --- | --- | --- |
| GFS 2026091512 | 49423 | 49425 | 49535 | 49594 |
| IFS 2026091512 | 49424 | 49442 | 49501 | 49577 |

## Published identity and readable data

At 23:03Z, node-27 `/api/v1/mvp/qhh/latest-product?source=gfs` and `source=ifs`
both selected `2026-09-15T12:00:00Z`, ready/published, with 71/71 stations and
1633/1633 segments. The discharge cycles endpoints selected the same newest
cycle. These are later than both pre-recovery source frontiers.

At 23:06Z, actual run-specific `q_down` MVT requests at z3/x6/y3 returned HTTP200
and decoded with GDAL. Both contained 1616 tile features; the tile is a spatial
subset, not an assertion of whole-network count. The first decoded segment was
`basins_qhh_shud_shud_riv_000001`:

| Source | Model / exact run | Discharge (m3/s) | Quality |
| --- | --- | --- | --- |
| GFS | `dg_0883c7e9c1006c6fd347df500315e9df` / `fcst_gfs_2026091512_dg_0883c7e9c1006c6fd347df500315e9df` | 0.00412100347222222 | qc_warning |
| IFS | `dg_9ccb261a39d51c24f4de9173fb4461b6` / `fcst_ifs_2026091512_dg_9ccb261a39d51c24f4de9173fb4461b6` | 0.00391779513888889 | ok |

Decoded variable, valid_time and run_id matched each requested new product.
No display source/config/role changes were made. This receipt proves the
requested dual-source upstream-to-publication progression, not all-backlog
catch-up, global scientific QC success or a new full C1–C4 security audit.
At 23:02Z the timer continued normal execution with state-save jobs in flight.

## Evidence locations

Raw outputs and source/test evidence accompany
`openspec/changes/preserve-pre-forecast-retry-stages/evidence/` (moved with the
change when archived). Key files: `deployment-patch.txt`, `node27-green.txt`,
`node22-deploy.txt`, `node22-deployed-smoke.txt`, `node22-first-pass-monitor.txt`,
`node27-gfs-publication-monitor.txt`, `node22-ifs-acceptance-probe.txt`,
`node22-ifs-forcing-completion.txt`, `node22-recovered-stage-identities.txt`,
`node27-dual-source-latest.txt`, `node27-dual-source-decoded-tiles.txt`.
