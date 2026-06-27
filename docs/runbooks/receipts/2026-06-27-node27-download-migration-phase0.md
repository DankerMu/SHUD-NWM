# Node-27 Download Migration Phase 0 Receipt

Captured: 2026-06-27T11:01:29Z

Scope: pre-migration live evidence for
`migrate-downloads-to-node27-retire-node22-db` tasks 1.1 and 1.2.

## Summary

- node-22 still runs the production scheduler:
  `services.orchestrator.cli plan-production --submit --continuous --max-passes 1`.
- node-22 still listens on historical PostgreSQL `:55433`, and scheduler-side
  postgres client processes are connected to `10.0.2.100:55433`.
- node-22 Slurm Gateway and diagnostic API are active:
  `services.slurm_gateway` on `127.0.0.1:8090` and FastAPI on `0.0.0.0:8001`.
- node-22 compute env still contains
  `DATABASE_URL=postgresql://<redacted>@10.0.2.100:55433/nhms`.
- node-27 hosts active DB/display: PostgreSQL `:55432`, display API
  `127.0.0.1:8080`, cron-driven `node27_autopipe`, and public
  `https://test.nwm.ac.cn`.
- node-27 ingest env points to writer DB
  `postgresql://<redacted>@127.0.0.1:55432/nhms`; display env points to the same
  host/port with `NHMS_SERVICE_ROLE=display_readonly` and control mutations
  disabled.
- Public latest-product reports both GFS and IFS at
  `2026-06-26T12:00:00Z`, `status=ready`, `run_status=published`.

## Node-22 Evidence

Repository state:

```text
node=node-22
repo_head=7b34cef
repo_status=?? .agent-evidence/
```

Active relevant processes:

```text
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway
/scratch/frd_muziyao/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8001 --log-level info
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli plan-production --submit --continuous --max-passes 1
postgres
postgres: nhms nhms 10.0.2.100(...) idle
postgres: nhms nhms 10.0.2.100(...) idle in transaction
```

Ports:

```text
0.0.0.0:8001
127.0.0.1:8090
0.0.0.0:55433
[::]:55433
```

Slurm queue sample:

```text
JOBID 9594_0
NAME nhms_forcing
STATE R
NODE cn11
```

Sanitized compute env excerpt:

```text
DATABASE_URL=postgresql://<redacted>@10.0.2.100:55433/nhms
NHMS_SERVICE_ROLE=compute_control
OBJECT_STORE_PREFIX=s3://nhms
OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
WORKSPACE_ROOT=/scratch/frd_muziyao/nhms-prod/workspace
```

Interpretation: node-22 still has an active scheduler dependency on its
historical PostgreSQL. Do not stop `:55433` until node-27 owns source-cycle and
pipeline state.

## Node-27 Evidence

Repository state:

```text
node=node-27
repo_head=7b34ceff
repo_status=?? .nhms-work/;?? .python-version;?? apps/frontend/._dist;?? apps/frontend/dist.bak-20260615-234427/;?? apps/frontend/dist.bak-20260615-235046/;?? scripts/node27_ingest_all.py
```

Active relevant processes:

```text
/home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080
postgres
postgres: nhms_display_ro nhms ... idle
```

Ports:

```text
127.0.0.1:8080
0.0.0.0:55432
[::]:55432
```

Cron:

```text
*/10 * * * * /home/nwm/NWM/scripts/node27_autopipe_cron.sh >> /home/nwm/autopipe.log 2>&1
```

Sanitized ingest env excerpt:

```text
BASINS_ROOT=/home/ghdc/nwm/Basins
DATABASE_URL=postgresql://<redacted>@127.0.0.1:55432/nhms
NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest
OBJECT_STORE_PREFIX=s3://nhms
OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
```

Sanitized display env excerpt:

```text
DATABASE_URL=postgresql://<redacted>@127.0.0.1:55432/nhms
NHMS_DISPLAY_API_PORT=8080
NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true
NHMS_ENABLE_LIVE_POSTGIS_MVT=true
NHMS_SERVICE_ROLE=display_readonly
OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
```

Local health:

```json
{"status":"ok","service":"nhms-api","version":"0.1.0"}
```

Public latest-product identity:

```text
GFS source=GFS cycle_time=2026-06-26T12:00:00Z run_id=fcst_gfs_2026062612_basins_qhh_shud status=ready run_status=published
IFS source=IFS cycle_time=2026-06-26T12:00:00Z run_id=fcst_ifs_2026062612_basins_qhh_shud status=ready run_status=published
```

Interpretation: node-27 is the public display/data oracle and is already
serving the latest visible GFS/IFS cycle from node-27 DB/object-store state.

## Migration Consequence

Phase 1 should add a node-27 download runner/preflight without stopping
node-22 DB. Phase 5 retirement is safe only after node-27 owns source-cycle and
pipeline state and after node-22 compute jobs no longer inherit business
`DATABASE_URL`.

