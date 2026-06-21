# node-27 live receipt — issue #597 display API restart script

- Date: 2026-06-21
- Branch: `ops/issue-597-display-api-restart-script` HEAD `918a721`
- Operator account: `nwm@210.77.77.27` (BatchMode SSH, no sudo)
- Issue: [#597](https://github.com/DankerMu/SHUD-NWM/issues/597)
  Carried follow-up from PR
  [#596](https://github.com/DankerMu/SHUD-NWM/pull/596) root-cause section
  (hand-launched orphan uvicorn dropping `DATABASE_URL`).

## Phase 1 — initial dry-run (caught script bug, fix landed in 918a721)

First invocation against `9bcb1b7` exited 4 with
`uvicorn did not respond on 127.0.0.1:8080 within 20s`. Root cause:
script probed `/api/v1/health` (does not exist → 404 on healthy uvicorn).
Actual health endpoint is `/health` (root) per
[apps/api/main.py:1947](apps/api/main.py#L1947)
`_register_static_and_health_routes`.

Important: the restart itself succeeded — pid 2451691 was bound to 8080 and
serving `/api/v1/models` 200 — only the script's bind-wait probe was wrong.
Fix committed in `918a721`: probe `/health` (root) and bump retry from 20s → 30s.

## Phase 2 — clean run on `918a721`

Pre-run baseline:

```text
PRE uvicorn pid: 2451691  (the relaunched pid from the failed phase 1 run)
PRE /api/v1/models?limit=1 first_basin_id: basins_heihe
```

Script invocation (`bash scripts/ops/start-display-api.sh`):

```text
[start-display-api] repo_root=/home/nwm/NWM
[start-display-api] env_file=/home/nwm/NWM/infra/env/display.env
[start-display-api] DATABASE_URL=postgresql://<redacted>@127.0.0.1:55432/nhms
[start-display-api] NHMS_ENABLE_LIVE_POSTGIS_MVT=true
[start-display-api] target=127.0.0.1:8080  log=/tmp/display-api.log
[start-display-api] stopping prior uvicorn pid(s): 2451691
[start-display-api] relaunched pid=2453425 (log: /tmp/display-api.log)
[start-display-api] OK pid=2453425 basin_id=basins_heihe (smoke check passed)
exit=0
```

Post-run state:

```text
POST uvicorn pid: 2453425  (clean handover from 2451691)
POST /proc/2453425/environ (filtered, DATABASE_URL value redacted to scheme+host+db):
  DATABASE_URL=<redacted>@127.0.0.1:55432/nhms
  NHMS_SERVICE_ROLE=display_readonly
  NHMS_AUTH_MODE=production
  NHMS_ENABLE_LIVE_POSTGIS_MVT=true
POST /health probe: http=200 time=2.5ms
POST /api/v1/models?limit=1:
  count: 1
  first_basin_id: basins_heihe
  model_id: basins_heihe_shud
```

## Acceptance — issue #597

- [x] node-27 display-api restart is reproducible from a single command:
  `bash scripts/ops/start-display-api.sh` (exit=0).
- [x] Restart always sources `infra/env/display.env`: confirmed via
  `[start-display-api] env_file=...` log line + post-run `/proc/<pid>/environ`
  containing `DATABASE_URL`, `NHMS_ENABLE_LIVE_POSTGIS_MVT`,
  `NHMS_SERVICE_ROLE`, `NHMS_AUTH_MODE`.
- [x] Missing/changed env vars surface in service status, not in user-facing
  popup: confirmed by script preflight (would abort with explicit
  missing-keys list, no value leak) + post-launch smoke check
  (`jq .data.items[0].basin_id != null` would exit non-zero on PR #596
  regression class).
- [x] Existing diagnostic script `scripts/diagnostic/display-cold-waterfall.sh`
  can call the new restart path: **DEFERRED to follow-up issue
  [#612](https://github.com/DankerMu/SHUD-NWM/issues/612)**. PR #611 Phase 4
  cross-review surfaced that the initial "no-op confirmed" determination here
  was wrong — `scripts/diagnostic/display-cold-waterfall.sh:103` DOES contain
  an inline `setsid .venv/bin/python -m uvicorn apps.api.main:app ...`
  launcher inside `launch_uvicorn()`, AND lines 20/25/143/165 reference
  `/healthz` (which 404s same as the `/api/v1/health` bug PR #611 Phase 1
  dry-run exposed). Refactoring the diagnostic script to defer to
  `scripts/ops/start-display-api.sh` AND fixing the `/healthz` → `/health`
  drift is real follow-up work, but expanding PR #611 scope would dilute
  the single-responsibility operator restart wrapper change. Tracked
  honestly in issue #612 instead.

## Caveats

- The systemd unit option from issue #597 (option 1) was not pursued because
  the `nwm@210.77.77.27` operator account does not have sudo
  (`sudo: a password is required` under `BatchMode=yes`). Deferred to a
  follow-up issue when sudo coordination is arranged. The hand-launched venv
  uvicorn shape remains the canonical runtime on node-27 until that
  migration; this script makes that shape reproducible and contract-asserting,
  not durable across host reboots.
- Receipt produced against a non-empty `core.model_instance` (`basins_heihe`
  active model present). On a fresh DB the script's smoke check would log
  the `0 items` warning path and exit 0 with the operator-visible message
  "verify model registration separately before declaring restart healthy" —
  a known, intentional fallback path, not a bug.
