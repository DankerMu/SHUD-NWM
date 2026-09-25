# node-27 live receipt: readonly-smoke-bound-identity-and-display-restart-anchor (PR #2637)

Taken on node-27, 2026-09-25, from 16:57Z. PR #2637 was merged at `579b2a6f5ed0f1d78d99f9881ecee8df806bc7e6`, and every run below uses that SHA. The receipt follows tasks §5; this is the orchestrator's record.

| item | verdict | note |
|---|---|---|
| **#2282** decoy survives the canonical restart | **PASS** | §1 and §2 |
| **C1** deploy | **PASS** | §2 |
| **C2** deny-write + read routes | **PASS** | unattended; GFS; IFS; merged full-scope (§3). A first attempt without the display runtime env was `BLOCKED` on `job_logs` only (§3.1) |
| **C3** cross-plane identity, node-27 half | **PASS** for both sources | §4. The node-22 producer-bundle aggregator was not run (§4) |
| **C4** browser plus production acceptance | **PASS** | §5 |

Private records live in `/home/nwm/n-deploy-579b2a6f5/` (0700). Evidence bundles are in `/home/nwm/NWM/artifacts/issue2484-n/` (gitignored). Stage outputs are in `/home/nwm/tmp/n/live/`.

## 1. Decoy and red proof (before the pull; `pgrep` only reads)

A decoy stands in for a second checkout's display API (design D9):
- `/home/nwm/tmp/n/yd-NWM/.venv/bin/python` is a symlink to `/usr/bin/python3`;
- a stub `uvicorn/__main__.py` sleeps and binds no port;
- it was started detached as pid `2032212`, with cmdline `/home/nwm/tmp/n/yd-NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8081 --workers 2`.

At `4b7d1f43f`, before the pull (`s1-red-proof.txt`):

```text
--- old pattern: pgrep -af '\.venv/bin/python -m uvicorn apps\.api\.main:app'
1881017 /home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 --workers 2
2032212 /home/nwm/tmp/n/yd-NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8081 --workers 2
--- anchored: pgrep -af '^/home/nwm/NWM/\.venv/bin/python -m uvicorn apps\.api\.main:app'
1881017 /home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 --workers 2
--- repo root spellings
git=/home/nwm/NWM readlink=/home/nwm/NWM
```

So the old sweep would have signalled the decoy, and the anchored one does not. The repo root has one spelling, and the unit's `ExecStart` execs `/home/nwm/NWM/.venv/bin/python`. That is the condition under which this checkout's own orphans still match (design Context).

## 2. Deploy (C1)

From `s2-deploy.txt`:
1. `git status --porcelain` was empty, and `git pull --ff-only` took the checkout from `4b7d1f43f` to `579b2a6f5`.
2. `bash scripts/ops/start-display-api.sh` returned rc 0 and printed:
   - `no prior uvicorn process found` (systemd `stop` had already stopped this checkout);
   - `systemd relaunched main_pid=2032539`;
   - `OK systemd_main_pid=2032539 workers=2 basin_id=basins_sw_ylzb (smoke check passed)`.
3. `MainPID` moved from `1881017` to `2032539`.
4. **The decoy survived**: `decoy_alive=yes pid=2032212`, with its cmdline unchanged.
5. Afterwards, the anchored `pgrep` lists only `2032539 … --port 8080`.

C1 record `c1-approved.json` (sha256 `5ef3897f751a1b718a4055123378a240900d30fee3c241610b1983dcc2be4d28`):
- `status` is PASS, and `head_sha` = `reviewed_sha` = `579b2a6f5…`;
- `/health` 200;
- `/api/v1/runtime/config` 200 with `service_role display_readonly`, `control_mutations_enabled false`, `slurm_routes_enabled false`, `queue_depth_mode display_readonly_unavailable`;
- `/api/v1/slurm/health` 404;
- `https://test.nwm.ac.cn/` equals `apps/frontend/dist/index.html` byte for byte.

Since the new server started, `/tmp/display-api.log` has **0** HTTP 500 lines and 3324 lines with a 2xx/304 status. The earlier 500s in the file are older tile requests (the last one is at line 1883332; the new server starts at line 1911674).

Afterwards the decoy was stopped by pid, after checking its cmdline, and its tree was removed.

## 3. C2: `scripts/validate_readonly_db_boundary.py` under `nhms_display_ro`

The DSN was passed only through the environment (`NHMS_DISPLAY_READONLY_DATABASE_URL`). With `infra/env/display.env` sourced (see §3.1), the results are:

| run | run id | status | `lane_statuses` | routes | discovered identity |
|---|---|---|---|---|---|
| unattended (no identity flags) | `n-c2-unattended-579b2a6f5-env` | **PASS** rc 0 | deny_write PASS, read_routes PASS | 9/9 PASS | IFS `fcst_ifs_2026092500_dg_8a34ed2b…`, basin `basins_hlj`, cycle `2026-09-25T00:00:00+00:00`, its own job `…_forecast_reconciled_56788_0` |
| GFS (`NHMS_READONLY_DB_VALIDATION_SOURCE=GFS`) | `n-c2m-579b2a6f5-gfs` | **PASS** rc 0 | PASS / PASS | 9/9 PASS | `fcst_gfs_2026092500_dg_3b091ccc…` (stored `source_id = gfs`, reported `GFS`), `basins_hlj`, job `…_56773_0` |
| IFS | `n-c2m-579b2a6f5-ifs` | **PASS** rc 0 | PASS / PASS | 9/9 PASS | `fcst_ifs_2026092500_dg_8a34ed2b…`, `basins_hlj`, job `…_56788_0` |
| merged (`--merge-source-dir` ×2, declared GFS+IFS) | `n-c2m-579b2a6f5` | **PASS** rc 0 | PASS / PASS | 15 route records, all PASS | `display_identity` keys `GFS`, `IFS`; `declared_sources [GFS, IFS]` |

In every run:
- `permission_probe_summary = {operation_count 23, passed_denial_count 23, failed_mutating_count 0, blocked_count 0, target_count 12}`;
- both manual-action probes returned 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` (PASS).

The `cycle_time` echo arrives spelled `…Z` and is requested as `…+00:00`, and it no longer blocks. `latest_product` is requested with the discovered basin (`basins_hlj`) and returns 200.

summary.json sha256:
- merged: `2864494a1d29480015404bafcf683d878a1a7c69705be8f5f2dbce7bbed579e3`;
- GFS: `efdc1bde5525f114101e31b078c7fcee3d831ab2baaa7e7b8ef20c4e8720d881`;
- IFS: `b3dd1151815ac2de9d22ca855c5bc980320d222c1bab881bd7e97629f36eb3de`;
- unattended: `8ec1c8bde934dcea2a47bca85e2bf2ab02f6b1a4e2da9218706e803c691cab32`.

The baseline for comparison (proposal "Why", `4b7d1f43f`, unattended) was **FAIL**: three routes were BLOCKED on the `cycle_time` spelling, `latest_product` got a 404 for the wrong basin, and `job_logs` got a 409 from pairing the IFS run with a GFS job.

### 3.1 The first attempt, without the display runtime environment

This attempt had only the DSN exported (`n-c2-{unattended,gfs,ifs}-579b2a6f5`). Every run gave overall `BLOCKED` (rc 2): deny_write PASS, 8/9 routes PASS, and `job_logs` BLOCKED on `400 JOB_LOG_URI_UNSUPPORTED`, reason `published_root_missing`.

The validator drives the checkout's app **in process**.
Without `NHMS_PUBLISHED_ARTIFACT_ROOT` (set in `display.env`) its log reader cannot resolve `published://` URIs, while the deployed `:8080` can. This is a precondition of the lane's environment, not a route defect. The #2420 receipt's runs were made with the display env as well.

The C2 note in `docs/runbooks/node-27-bringup-checklist.md` now states it: source `display.env` first, then export the readonly DSN.

The merge also refused per-source ids that were not `<merged-id>-gfs/-ifs` (`READONLY_DB_MERGE_SOURCE_PARENT_RUN_MISMATCH`), so the runs were redone with the runbook's parent-bound ids (`n-c2m-…`). That is the existing merge rule, unchanged by this PR.

## 4. C3, node-27 half: one identity per source, from the DB to the browser

For each source, the identity the C4 browser lane displayed (§5) was pinned with `--strict-run-id` (`n-c3-579b2a6f5-{gfs,ifs}`):

| source | run (`hydro.hydro_run.status`) | basin / cycle | validator | identity-bound routes | the C4 browser showed the same identity | `/ops` job and logs |
|---|---|---|---|---|---|---|
| GFS | `fcst_gfs_2026092412_dg_0883c7e9c1006c6fd347df500315e9df` (`published`, stored `gfs`) | `basins_qhh` / `2026-09-24T12:00Z` | PASS, lanes PASS/PASS | all five PASS (latest_product, jobs, status, stages, job_logs) | **yes** (run, model, basin, cycle, source) | `…_forecast_reconciled_56449_0`, logs 200; `/` map surface visible |
| IFS | `fcst_ifs_2026092412_dg_9ccb261a39d51c24f4de9173fb4461b6` (`published`) | `basins_qhh` / `2026-09-24T12:00Z` | PASS, lanes PASS/PASS | all five PASS | **yes** | `…_forecast_reconciled_56418_27`, logs 200; map visible |

So for both sources, one strict `run_id`/`source`/`cycle_time`/`model_id`/`basin_id` chains through:
- the node-22-produced `hydro_run` row (`published`);
- its published job log (`job_logs` 200 with the echo);
- the strict latest-product;
- the `/` single-page map and `/ops`, which is what C4 shows.

What this does not claim: the checklist's full C3 also names the two-node producer-bundle aggregator (`validate_two_node_e2e_evidence.py`). That needs a node-22 producer bundle and was not run. This receipt is C3's node-27 half only.

## 5. C4: `test:e2e:live-c4-display` plus production acceptance

Inputs:
- `c1-c4-approval.json` (sha256 `0d427e036fa185b489ec8d4057816f570f122fc2387484fb15aee915457567ec`): `status PASS`, `head_sha` = `reviewed_sha` = `579b2a6f5…`, the C1 digest, and the GFS/IFS identities and job ids from §3.
- Pins: basin `basins_qhh`, segment `basins_qhh_shud_reach_000001`; origins `https://test.nwm.ac.cn`.

The run followed the checklist C4 sequence:
1. `freeze` — PASS.
2. `sleep 1`.
3. The browser lane: `1 passed (5.9s)`, `visits / and strict-identity /ops for GFS and IFS without role spoofing`.
4. `bind` — PASS, bracket `cmd_start 1790355613` / `cmd_end 1790355622`.
5. `verify` — **PASS**; `acceptance.json` has `stage verify`, `status PASS` and `reviewed_sha 579b2a6f5…`.

The C4 evidence file (`status PASS`) shows, per source:
- `/ops` `status_status`, `stages_status`, `jobs_status` and `logs_status` all 200;
- `role_selector_count`, `retry_cancel_control_count`, `slurm_request_count` and `non_get_control_count` all 0;
- on `/`, `map_surface_visible true`.

sha256:
- `freeze.json` `f750fab8ab546029ee804fd73d5d2e8687dad0d87897337b53ef6af29cf34d48`;
- `binding.json` `b4c1c550c5990038b141111dfa23710d48f491c645fb5e2e946aa9de7e88f9f3`;
- `acceptance.json` `3780d8acef5b36a54ccaa416a46bb16531111e2d1b6c9aa47f5152bb2feaca77`;
- `nhms-frontend-c4-live-evidence-n.json` `6ecdc1bda72323048f737217d97806c3081ecad113ab31de4c7d8e0a646ca56a`.

## Premise correction

The acceptance in #2484 assumed "job_logs stays BLOCKED on #2420". That premise is out of date: #2420 closed on 2026-09-20 with published job provenance. On this deploy, `job_logs` PASSes for every discovered and every pinned identity, for both sources.
