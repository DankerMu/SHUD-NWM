# Node-27 IFS Download Live Proof

Captured: 2026-06-27

Scope: additional dual-source evidence for
`migrate-downloads-to-node27-retire-node22-db` task 2.3.

## Summary

- node-27 was on branch `codex/facade-shrink-scheduler-chain` at `d8462da9`.
- The node-27 bounded download runner used the same data-plane writer env and
  user-local GRIB environment proven by the GFS receipt:
  `/home/nwm/nhms-grib/bin/cdo`.
- The run downloaded IFS cycle `2026-06-26T12:00:00Z` successfully from node-27.
- Artifacts were written to shared NFS under
  `/home/ghdc/nwm/object-store/raw/IFS/2026062612`, visible from node-22 as
  `/ghdc/data/nwm/object-store/raw/IFS/2026062612`.
- node-27 active DB `met.forecast_cycle` contains the same raw manifest identity
  with `status=raw_complete`.

## Runner

Invocation:

```bash
cd /home/nwm/NWM
set -a
. infra/env/node27-ingest.env
set +a
export NHMS_NODE27_DOWNLOAD_ROLE=node27_data_plane_download
export NHMS_SERVICE_ROLE=node27_data_plane_download
export WORKSPACE_ROOT=/home/nwm/node27-download-work
export NODE27_DOWNLOAD_LOG_ROOT=/home/nwm/node27-download-logs
export NODE27_DOWNLOAD_LOCK_PATH=/tmp/node27-download.lock
export NHMS_NODE27_DOWNLOAD_ALLOWED_CYCLE_HOURS_UTC=0,12
export NHMS_DOWNLOAD_BBOX_SOUTH=8
export NHMS_DOWNLOAD_BBOX_NORTH=64
export NHMS_DOWNLOAD_BBOX_WEST=63
export NHMS_DOWNLOAD_BBOX_EAST=145
export NODE27_DOWNLOAD_ALLOWED_DATABASE_ENDPOINTS=127.0.0.1:55432,localhost:55432
export NHMS_GRIB_ENV_ROOT=/home/nwm/nhms-grib
.venv/bin/python scripts/node27_download_cycles.py \
  --cycle-time 2026-06-26T12:00:00Z \
  --source IFS \
  --summary-path /home/nwm/node27-download-logs/ifs-2026062612-summary.json
```

Summary:

```json
{
  "cycle_time": "2026-06-26T12:00:00Z",
  "downloads": {
    "downloaded": 1,
    "failed": 0,
    "processed": 1
  },
  "return_code": 0,
  "schema": "nhms.node27_download.summary.v1",
  "sources": [
    "IFS"
  ],
  "status": "completed"
}
```

Source detail:

```json
{
  "command": [
    "/home/nwm/NWM/.venv/bin/nhms-ifs",
    "download",
    "--cycle-time",
    "2026-06-26T12:00:00Z"
  ],
  "result": {
    "files": 424,
    "retry_count": 0,
    "status": "raw_complete",
    "total_bytes_written": 52600582
  },
  "return_code": 0,
  "source": "IFS",
  "status": "downloaded"
}
```

## Raw Artifacts

Node-27 view:

```text
/home/ghdc/nwm/object-store/raw/IFS/2026062612
physical_file_count=54
manifest=/home/ghdc/nwm/object-store/raw/IFS/2026062612/manifest.json
manifest_size=448K
owner=nwm:nwm
```

Manifest identity:

```json
{
  "cycle_time": "2026-06-26T12:00:00+00:00",
  "entry_count": 424,
  "manifest_uri": "s3://nhms/raw/IFS/2026062612/manifest.json",
  "source_id": "IFS"
}
```

Node-22 shared NFS view:

```text
/ghdc/data/nwm/object-store/raw/IFS/2026062612
physical_file_count=54
manifest_owner_uid_gid=1005:1005
```

## Node-27 DB Proof

Queried through `psycopg2` using `infra/env/node27-ingest.env` writer
`DATABASE_URL`.

```json
{
  "created_at": "2026-06-27T11:40:32.657439+00:00",
  "cycle_time": "2026-06-26T12:00:00+00:00",
  "error_code": "",
  "manifest_uri": "s3://nhms/raw/IFS/2026062612/manifest.json",
  "retry_count": 0,
  "source_id": "IFS",
  "status": "raw_complete"
}
```

## Consequence

Node-27 now has live proof for both production sources on the same safe cycle:
GFS and IFS can write raw source manifests and active DB source-cycle state
without connecting to node-22 historical PostgreSQL.

