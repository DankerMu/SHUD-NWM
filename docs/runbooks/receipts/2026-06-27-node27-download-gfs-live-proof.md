# Node-27 GFS Download Live Proof

Captured: 2026-06-27

Scope: `migrate-downloads-to-node27-retire-node22-db` task 2.3.

## Summary

- node-27 code was fast-forwarded to branch
  `codex/facade-shrink-scheduler-chain` at `af4ad23b`.
- node-27 initially failed download preflight only on
  `GRIB_TOOL_CDO_MISSING`; DB identity, paths, lock, bbox, and cycle-hour config
  were ready.
- A user-local GRIB environment was installed on node-27:
  `/home/nwm/nhms-grib/bin/cdo`, CDO `2.6.1`, via micromamba under `/home/nwm`.
- Shared NFS `object-store/raw` did not exist or was not writable by node-27
  `nwm` (`uid=1005`). Because NFS ACL was unsupported, node-22 owner created
  `/ghdc/data/nwm/object-store/raw` and set mode `0777`. Other object-store
  surfaces (`runs`, `forcing`, `published`) were not opened.
- node-27 then ran the bounded download runner for GFS cycle
  `2026-06-26T12:00:00Z` successfully.
- The run wrote node-27-visible raw artifacts under
  `/home/ghdc/nwm/object-store/raw/gfs/2026062612` and node-22-visible artifacts
  under `/ghdc/data/nwm/object-store/raw/gfs/2026062612`.
- node-27 active DB `met.forecast_cycle` contains the same raw manifest identity
  using node-27 writer credentials.

## Commands

Node-27 live runner invocation:

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
export PATH=/home/nwm/nhms-grib/bin:$PATH
.venv/bin/python scripts/node27_download_cycles.py \
  --cycle-time 2026-06-26T12:00:00Z \
  --source GFS \
  --summary-path /home/nwm/node27-download-logs/gfs-2026062612-summary.json
```

The committed runner now injects `NHMS_GRIB_ENV_ROOT/bin` into subprocess PATH,
so the explicit PATH export above is no longer required after `af4ad23b` plus
the follow-up PATH-injection commit are deployed.

## Preflight

Initial blocker:

```text
status=preflight_blocked
blocker=GRIB_TOOL_CDO_MISSING
database=127.0.0.1:55432/nhms writer_candidate
object_store_root=/home/ghdc/nwm/object-store
workspace_root=/home/nwm/node27-download-work
lock=/tmp/node27-download.lock
bbox=8,64,63,145
cycle_hours=0,12
```

After installing `/home/nwm/nhms-grib`, preflight:

```text
status=preflight_ready
cdo=/home/nwm/nhms-grib/bin/cdo
database=127.0.0.1:55432/nhms writer_candidate
blockers=[]
```

## Download Summary

Runner summary:

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
    "GFS"
  ],
  "status": "completed"
}
```

Source detail:

```json
{
  "command": [
    "/home/nwm/NWM/.venv/bin/nhms-gfs",
    "download",
    "--cycle-time",
    "2026-06-26T12:00:00Z"
  ],
  "result": {
    "files": 397,
    "retry_count": 0,
    "status": "raw_complete",
    "total_bytes_written": 47690539
  },
  "return_code": 0,
  "source": "GFS",
  "status": "downloaded"
}
```

The GFS adapter emitted duplicate APCP cumulative `.idx` selection warnings for
early forecast hours, then completed successfully. No credential material was
printed.

## Raw Artifacts

Node-27 view:

```text
/home/ghdc/nwm/object-store/raw/gfs/2026062612
physical_file_count=58
manifest=/home/ghdc/nwm/object-store/raw/gfs/2026062612/manifest.json
manifest_size=580K
owner=nwm:nwm
```

Manifest identity:

```json
{
  "cycle_time": "2026-06-26T12:00:00+00:00",
  "entry_count": 397,
  "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
  "source_id": "gfs"
}
```

Node-22 shared NFS view:

```text
/ghdc/data/nwm/object-store/raw/gfs/2026062612
physical_file_count=58
manifest_owner_uid_gid=1005:1005
```

## Node-27 DB Proof

Queried through `psycopg2` using `infra/env/node27-ingest.env` writer
`DATABASE_URL`; `psql` was not on node-27 PATH.

```json
[
  {
    "created_at": "2026-06-27T11:14:03.805125+00:00",
    "cycle_time": "2026-06-26T12:00:00+00:00",
    "error_code": "",
    "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
    "retry_count": 0,
    "source_id": "gfs",
    "status": "raw_complete"
  }
]
```

## Follow-Up

Phase 2 can now build on a proven node-27 download substrate. The remaining
production cutover work is to make 27 select cycles automatically, run IFS live
proof, and disable node-22 production `download_source_cycle` only after 27
owns source-cycle truth for both sources.

