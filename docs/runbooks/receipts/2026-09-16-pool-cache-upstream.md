# Receipt: #2346 pool first step + #2431 PNG cache-root + #2432 diagnosis

- Date: 2026-09-16 (UTC). Node-27 local offset `+08:00`.
- Operational window: **2026-09-16T10:33:20Z → 10:34:11Z** (pool apply through retention execute). Validator pins followed at 10:34:50Z / 10:36:31Z / 10:37:51Z.
- Backup: `/home/nwm/nhms-ops-backup-2346-2431-20260916T103321Z/`.
- Isolated worktree used to prepare docs/runner: `/tmp/nwm-2346-2431-2432`.
- Throwaway runner prepared locally at `/tmp/nwm-ops-2346-2431.py`; parent uploaded and executed `/home/nwm/tmp/nwm-ops-2346-2431.py`.
- OpenSpec change: `configure-display-pool-cache-pruning-diagnose-forcing`.
- DeployPreparation did **not** execute remote mutations. Operational sections
  are filled only from parent-returned node-27 output.

## 0. Completion boundary

| Issue | This batch | Remains open |
|---|---|---|
| #2346 | First-step pool 8+8 per existing worker, same-code restart, smoke | Isolation / prewarming / role SQL |
| #2431 | Set `NHMS_MVT_FILE_CACHE_DIR` on retention env to the display process cache root; plan then one production tick | None for the key itself |
| #2360 | Recorded: lock untouched; `lock_unsafe` **not exercised** (no expired canonical) | Lock owner / unit uid |
| #2432 | Read-only diagnosis of exact candidate | Pipeline recovery |
| #2439 | Linked as runtime repair owner for the diagnosed scheduler defect | Repair implementation |

No source/tests/config-template changes. `infra/env/display.example` pool
defaults 4/2 and `node27-raw-retention.example` cache-root line are already
correct; production files were missing the live keys.

## 1. Baseline (supplied preflight; not this window's mutations)

Captured 2026-09-16T09:56:34Z on node-27 `/home/nwm/NWM`.

- HEAD `7ecc46bed18cc8b6f20030f49aa3bf43a77b6858` (master). Display
  `MainPID=2901698` started `2026-09-16 16:15:16 CST`, immediately after that
  checkout. `WorkingDirectory=/home/nwm/NWM`, `--workers 2`, no drop-ins.
- Disk: `/` 98G 81% (18G avail); `/home` 1.7T 31%; `/data/GHDC` 15T 15%.
- `display.env` and `node27-raw-retention.env`: mode `600`, uid `1005`.
- Canonical lock `/home/ghdc/nwm/object-store/.nhms-copyback-batch.lock`:
  mode `600`, uid `1103` gid `1078`. Untouched by this batch.
- Display env keys `NHMS_DISPLAY_DB_POOL_SIZE` /
  `NHMS_DISPLAY_DB_MAX_OVERFLOW` / `NHMS_DISPLAY_WORKERS` /
  `NHMS_MVT_FILE_CACHE_DIR` **absent** in the file.
  Process: pool keys `<unset>` (code default 4+2),
  `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt` from unit fallback,
  `NHMS_SERVICE_ROLE=display_readonly`.
- Retention env present: `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`,
  `SOURCES=GFS,IFS`, `DAYS=14`. `NHMS_MVT_FILE_CACHE_DIR` **absent**.
- SQL via installed sqlalchemy / display DSN (credentials not printed):
  `max_connections=100`, `superuser_reserved_connections=3`,
  `current_user=nhms_display_ro`.
  `pg_stat_activity`: `nhms` 4, `nhms_display_ro` active 1 + idle 10,
  other/none 4.
- Runtime `GET /api/v1/runtime/config`:
  `service_role=display_readonly`, `control_mutations_enabled=false`,
  `slurm_routes_enabled=false`, `queue_depth_mode=display_readonly_unavailable`,
  `display_readonly=true`.

Capacity formula used by the runner (re-measured at apply time, not this
snapshot):

```text
workers × (pool + overflow) + other_demand + reserved + explicit_headroom
    ≤ max_connections
```

Desired display budget if workers remain 2: `2 × (8 + 8) = 32`.
`other_demand` keeps `nhms_display_ro` sessions that lack
`application_name=nhms-display-api`. Snapshot arithmetic is **not**
admission. Isolation is **not** claimed if 8+8 is applied.

## 2. #2346 pool apply (parent-executed)

Status: **applied**. Parent ran:

```bash
cd /home/nwm/NWM && uv run --no-sync python /home/nwm/tmp/nwm-ops-2346-2431.py pool-apply
cd /home/nwm/NWM && uv run --no-sync python /home/nwm/tmp/nwm-ops-2346-2431.py verify
```

DeployPreparation did not mutate the node.

Window start: 2026-09-16T10:33:20Z. Host `ghdc`. Backup
`/home/nwm/nhms-ops-backup-2346-2431-20260916T103321Z/`
(display.env copy under `home/nwm/NWM/infra/env/display.env`).

| Item | Result |
|---|---|
| HEAD before/after | `7ecc46bed18cc8b6f20030f49aa3bf43a77b6858` master, porcelain 0 |
| Restart | `systemctl --user restart nhms-display-api.service` only, 2.4 s |
| MainPID | 2901698 → **3023964**, cwd `/home/nwm/NWM`, `--workers 2` |
| Added keys | `NHMS_DISPLAY_DB_POOL_SIZE=8`, `NHMS_DISPLAY_DB_MAX_OVERFLOW=8` |
| Effective process | pool 8, overflow 8, cache `/home/nwm/.cache/nhms/mvt`, role `display_readonly` |
| Isolation | **false** (explicit in runner JSON) |

Admission (formula
`workers*(pool+overflow) + other_demand + reserved + explicit_headroom <= max_connections`):

| | max | reserved | other_demand | desired | headroom | needed | total_sessions | display_attributed | uncertain_ro |
|---|---|---|---|---|---|---|---|---|---|
| before apply | 100 | 3 | 11 | 32 | 8 | 54 admitted | 19 | 8 | 3 |
| immediately after restart | 100 | 3 | 9 | 32 | 8 | 52 admitted | 9 | 0 | 1 |
| later capacity snapshot | 100 | 3 | — | — | — | — | nhms 4 + display_ro active 1 idle 12 + none 4 = 21 | process still 8+8 | — |

Later snapshot:

(`openspec/changes/archive/2026-09-16-configure-display-pool-cache-pruning-diagnose-forcing/evidence/node27-pool-after-capacity.txt`): env file now has pool 8/8;
workers and cache keys remain absent from `display.env` (unit/process
fallback still supplies cache `/home/nwm/.cache/nhms/mvt`); retention
env now includes `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt`.
Immediate post-restart `display_attributed=0` is a restart gap, not a
pool failure; later grouped sessions returned to 21.

Verify (`pid=3023964`, published cycles discovered, not invented):

| Probe | HTTP | bytes | ttfb s |
|---|---|---|---|
| `/health` | 200 | 54 | 0.002 |
| `/` | 200 | 646 | 0.010 |
| `/api/v1/layers` | 200 | 6073 | 0.129 |
| `river-network-national/4/12/6.pbf` | 200 | 149416 | 0.023 |
| gfs `2026-09-14T00:00:00Z` q_down lead0 z4/12/6 | 200 | 1374311 | 0.053 |
| ifs `2026-09-14T12:00:00Z` q_down lead0 z4/12/6 | 200 | 1374336 | 0.026 |
| `/api/v1/slurm/health` | **404** | 22 | 0.004 |

Runtime: `service_role=display_readonly`, `display_readonly=true`,
`control_mutations_enabled=false`, `slurm_routes_enabled=false`,
`queue_depth_mode=display_readonly_unavailable`.

SQLAlchemy deny-write (explicit transaction): `current_user=nhms_display_ro`,
`sqlstate=42501`, `denied=true`, rolled back. This is not a latency or
cold-isolation claim.

Public nginx (`openspec/changes/archive/2026-09-16-configure-display-pool-cache-pruning-diagnose-forcing/evidence/public-network-smoke.json`): `/` 200 646 B,
`/api/v1/layers` 200 6073 B, `river-network-national/4/12/6.pbf` 200
149416 B.

Canonical readonly validator **ran on the installed CLI** (`--help` rc 0;
live mode, not dependency-blocked). The verify runner's leftover sentence
about `psycopg2` module-load is **superseded** by that CLI success and is
not a pool regression.

1. Unpinned `--source gfs` (`pool-2346-gfs-20260916`, 10:34:50Z) auto-selected
   IFS identity `fcst_ifs_2026091412_dg_f7c50f9511499355e8ca7bd6789b0a23`
   while declaring `source=gfs`. Latest-product 404
   `QHH_LATEST_PRODUCT_UNAVAILABLE`; jobs/status/stages 404
   `PIPELINE_STRICT_IDENTITY_NOT_FOUND`. Pinning error, not a pool change.
2. Pinned GFS (`pool-2346-gfs-pinned-20260916`, 10:36:31Z; `openspec/changes/archive/2026-09-16-configure-display-pool-cache-pruning-diagnose-forcing/evidence/node27-readonly-gfs.json`) identity
   `fcst_gfs_2026091400_dg_0883c7e9c1006c6fd347df500315e9df` /
   `2026-09-14T00:00:00Z`. Role `nhms_display_ro`,
   `transaction_read_only=off`, mutating/reachable/unsafe findings empty.
   Permission matrix 23/23 denials on 12 targets. retry/cancel 409 PASS
   `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`. Overall **BLOCKED** solely
   because jobs / pipeline_status / pipeline_stages returned HTTP 200
   without strict identity in the bodies, and job_logs BLOCKED.
3. Pinned IFS (`pool-2346-ifs-pinned-20260916`, 10:37:51Z; `openspec/changes/archive/2026-09-16-configure-display-pool-cache-pruning-diagnose-forcing/evidence/node27-readonly-ifs.json`) identity
   `fcst_ifs_2026091412_dg_9ccb261a39d51c24f4de9173fb4461b6` /
   `2026-09-14T12:00:00Z`. Same 23/23 denials, same 409 PASS, same overall
   BLOCKED residual on jobs/pipeline/stages identity + job_logs.

[INFERENCE] That wider validator BLOCKED is a response-identity residual of
unchanged runtime code: this batch did not change source, and no before
validator was run, so “pre-existing” is not a before/after measurement.
It is **not resolved and not required** for this env-only slice. Fixture
needs applicable live config / readonly boundary; localhost smoke plus
SQLSTATE 42501 covers the pool change. Not C1 docker, not C3 cross-plane,
not C4 browser, not full C2 PASS. Existing runtime code unchanged.

No pool rollback.

## 3. #2431 PNG cache-root (parent-executed)

Status: **PNG lane activated**. Zero expired candidates after both
configured sources were safely evaluated. Canonical `#2360`
`lock_unsafe` was **not exercised** (no expired canonical targets).

Plan 2026-09-16T10:33:46Z: timer `nhms-node27-raw-retention.timer`
stopped only (`ActiveState=inactive`, `UnitFileState=enabled`). Added
only `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt`. Wrapper
`NODE27_RAW_RETENTION_PLAN_ONLY=true` after asserting the env file does
not assign that flag. Wrapper rc=0.

| | plan | execute |
|---|---|---|
| time | 10:33:46Z | 10:34:11Z |
| summary | `/home/nwm/nhms-ops-backup-2346-2431-20260916T103321Z/raw-retention-plan-20260916T103346Z.json` | `/home/nwm/node27-raw-retention-logs/raw-retention-20260916T103411Z.json` (from log `done summary=`, not latest-file) |
| sha256 | `b0b50ce198aa584b0d71bfdf9f7362ee49f972472ac08abde04247d2dc4df1f1` | `86e8423b0f2aeb07a221977a82cc5abbbe20646e1038537a9ff68fb2348dbff5` |
| execution_mode | `plan_only` dry_run true | `production_execute` dry_run false |
| precip_cache_root | `/home/nwm/.cache/nhms/mvt` | same |
| counts | planned 0 deleted 0 failed 0 skipped 127 | same |
| PNG planned/deleted/failed | 0 / 0 / 0 | 0 / 0 / 0 |
| PNG skips | `precip-cache/gfs/2026091400` and `precip-cache/IFS/2026091412` both `within_retention_window` | same |
| unsafe/missing PNG skips | none | none |
| sources evaluated | gfs, IFS | gfs, IFS |
| watermark | reference `2026-09-14T12:00:00Z`, cutoff `2026-08-31T12:00:00Z`, mode `display_watermark` | production tick |
| service | — | `systemctl start` rc 0, Result=`success`, ExecMainStatus=`0`, ActiveState=`inactive` |
| lock | uid 1103 mode 600 untouched | same; `canonical_rc1_expected_issue_2360=false` |
| timer | left stopped for inspection | restored `active` / unit_file `enabled` |

Zero expired PNG is reported as zero. Object-store root / sources /
days unchanged. No cache fixtures. No rollback.

## 4. Rollback

Not used. Commands remain:

```bash
cd /home/nwm/NWM && uv run --no-sync python /home/nwm/tmp/nwm-ops-2346-2431.py pool-rollback
cd /home/nwm/NWM && uv run --no-sync python /home/nwm/tmp/nwm-ops-2346-2431.py retention-rollback
```

Rollback would restore only this operation's keys and refuse if those
keys were changed by someone else.


## 5. #2432 upstream diagnosis (read-only; not recovery)

Repair owner: https://github.com/DankerMu/SHUD-NWM/issues/2439
(#2432 may close as diagnosis completed; production is not restored).

### Exact candidate and red signal

Evidence file:
`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/scheduler_2026091609_34d1f52653c6.json`
(`started_at` 2026-09-16T09:53:37.613006Z; that timestamp is the evidence
header / pass start, not `finished_at`). Active tree HEAD
`7b38bcb8bebd65ce051dae0771c896e1ca96c0fc`. Runtime is file-backed
(`database_url_configured: false`).

Candidate
`gfs:2026-09-14T12:00:00Z:dg_0883c7e9c1006c6fd347df500315e9df:forecast_gfs_deterministic`
(`cycle_id=gfs_2026091412`).

Executed failing diagnostic (no DB, no env rebuild): `/tmp/nwm-2432-red.py`
against that JSON. rc=1:

- `forcing_provenance`: `source=absent`, `tier_status=sidecar_absent`
- `artifact_guard.artifact_exists=false`
- `stable_classifier=FORCING_VERSION_ROW_ABSENT`
- `planned_retry_reason=strict_warm_start_retry_run_manifest_mismatch`
- `pipeline_jobs`: one succeeded `convert_canonical` Slurm 48756; **no
  forcing job recorded**
- `retry_policy.attempt=0`

`FORCING_VERSION_ROW_ABSENT` is the file-backend missing-witness
classifier. It does **not** prove a missing PostgreSQL row.
Pass counts: 38 candidate observations, 38 blocked, 0 submitted. Those
38 observations share one succeeded `convert_canonical` cohort job
`job_cycle_gfs_2026091412_convert_cohort_4f6d17de405b_convert` Slurm
48756 (`jobs_by_stage` Counter convert/succeeded=38 is not 38 distinct
Slurm jobs). No forcing job. `dry_run=false`,
`execution_mode=production_orchestration`. `no_progress_circuit.open`
contains both older `subject_kind=job` entries with
`ambiguous_fallback_match:comment_accounting_unproven` and
`subject_kind=candidate` entries, including this exact GFS candidate
with `reason=blocked:forcing_version_row_absent` and
`consecutive_passes=185` (plus siblings). The forcing-witness guard is
the observed current candidate blocker. Circuit behavior was neither
replayed nor bypassed here and remains a recovery consideration for #2439.

### Ranked hypotheses after the red signal

1. Retry classification fires before first forcing submission: a
   convert-only state is upgraded to forecast retry, then the missing
   forcing guard blocks it. Predict: successful convert, no forcing
   job, strict-warm mismatch on a shared cohort run. **Supported.**
2. Forcing producer failed before publication. Predict a failed forcing
   job/log for this candidate. **Ruled out for this candidate** — no
   forcing job exists in `pipeline_jobs`.
3. Producer/reader root or witness identity mismatch. Predict a package
   elsewhere or sidecar/journal mismatch. **Not evidenced** here;
   provenance is `sidecar_absent`, URI null.
4. Global execution gate disables submission. Predict dry-run / disabled
   / circuit rather than candidate-local missing-forcing. **Does not
   independently explain the captured candidate decision**: the pass is
   production_orchestration with `dry_run=false`, while the artifact guard
   blocks this candidate. Candidate-level no-progress tracking exists for
   it; its recovery implications remain for #2439. This diagnosis neither
   replayed nor bypassed circuit behavior.
Later GFS cycles `gfs_2026091500` / `gfs_2026091512` are
`backfill_deferred_waiting_for_prior_cycle`. Observed IFS:
`ifs_2026091500` is `backfill_deferred_waiting_for_global_prior_cycle`;
`ifs_2026091512` is `backfill_deferred_waiting_for_prior_cycle`. Those
deferrals close both-source diagnosis of why later cycles did not
advance; they do **not** prove an IFS forcing producer failed.

### Pure-function replay scope (not a production tick)

Two read-only replays were executed against captured JSON plus active
`/scratch/frd_muziyao/NWM` helpers. Neither submits work, neither
replays the full scheduler pass, neither covers IFS.

1. Helper-only (`/tmp/nwm-2432-replay.py`): manually constructed
   `CandidateStateDecision(retry, resume_after_completed_stage,
   restart_stage=forcing)` fed to
   `_upgrade_retry_for_strict_warm_start_manifest`. Output:
   `restart_stage=forecast`,
   `reason=strict_warm_start_retry_run_manifest_mismatch`,
   `manifest_in_evidence=false`. This proves the helper rewrite only.
2. Decision-chain (`/tmp/nwm-2432-chain-replay.py`, stronger):
   rehydrates captured provider members
   `hydro_run`, `forcing_version`, `forecast_cycle`, `pipeline_jobs`,
   `pipeline_events`, `completed_stage_evidence` (excluding downstream
   decision annotations). Active `_candidate_state_decision` →
   `retry` / `resume_after_completed_stage` / `forcing`;
   `_upgrade_retry_for_strict_warm_start_manifest` → `forecast` /
   `strict_warm_start_retry_run_manifest_mismatch`;
   `_strict_warm_start_forcing_witness_decision` → `blocked` /
   `FORCING_VERSION_ROW_ABSENT` with the same planned retry. Assertion
   that the upgrade keeps `forcing` failed red.

Root cause for this GFS candidate: convert completed; the strict-warm
upgrade rewrites a forcing resume into a forecast retry because the
captured evidence has no matching run manifest; the forcing-witness
guard then blocks on an absent sidecar. Downstream symptom on node-27
is stale published cycles (gfs `2026-09-14T00:00:00Z` / IFS
`2026-09-14T12:00:00Z` at the #2017 window). IFS producer failure is
**not** proven by this GFS-only replay.

This batch does not submit, retry, rebuild forcing, or change the
node-22 environment.

## 6. Deviations and limits

- Env templates left at defaults (display example 4/2; retention example
  already names `/home/nwm/.cache/nhms/mvt`).
- No cache fixture, no lock workaround, no `uv sync`, no source/test
  changes, no OpenSpec checkbox edits.
- #2360 `lock_unsafe` was not observed this tick because nothing canonical
  had expired; the lock file remained 0600 uid 1103.
- Wider readonly validator BLOCKED on jobs/pipeline/stages identity and
  job_logs is recorded, not repaired. Unpinned GFS-declared / IFS-selected
  identity is recorded as operator pinning, not a pool regression.
- Raw runner `c1_c4_note` claiming psycopg2 module-load blocked the
  canonical validator is superseded by the live CLI runs above.
- Isolation / prewarming / role SQL remain #2346 second step. #2432
  diagnosis is not recovery; repair owner is #2439.

## 7. Related

- Runbook: `docs/runbooks/display-readonly-live-mvt.md` (pool formula,
  PNG/PBF pairing, #2360 separation).
- Prior PNG growth observation: `docs/runbooks/receipts/2026-09-16-display-v2.md` §6/§8.
- [Fixture evidence directory](../../../openspec/changes/archive/2026-09-16-configure-display-pool-cache-pruning-diagnose-forcing/evidence/)
  (`node27-pool-apply.txt`, `node27-pool-verify.txt`,
  `node27-pool-after-capacity.txt`, `node27-retention-plan.txt`,
  `node27-plan-inspection.txt`, `node27-retention-execute.txt`,
  `node27-readonly-gfs.json`, `node27-readonly-ifs.json`,
  `public-network-smoke.json`, `node22-forcing-diagnosis.txt`,
  `node22-decision-chain-replay.txt`).
- Repair issue: https://github.com/DankerMu/SHUD-NWM/issues/2439
