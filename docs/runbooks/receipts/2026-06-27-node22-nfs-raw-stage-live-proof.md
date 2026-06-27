# Node-22 NFS Raw Stage Live Proof

Captured: 2026-06-27

Scope: `migrate-downloads-to-node27-retire-node22-db` task 4.5.

## Summary

- node-22 repository was fast-forwarded to branch
  `codex/facade-shrink-scheduler-chain` at `2e14698`.
- The node-22 runtime env now requires node-27 raw manifests and stages raw
  inputs into a compute-visible object-store before downstream Slurm submit.
- A Slurm compute-node read check on `cn11` proved the important boundary:
  `/ghdc/data/nwm/object-store/...` is not readable from compute, while
  `/scratch/frd_muziyao/nhms-prod/object-store/...` is readable.
- GFS and IFS `2026-06-26T12:00:00Z` raw manifests from node-27 were staged
  from shared NFS into `/scratch/frd_muziyao/nhms-prod/object-store`.
- The node-22 compute API was restarted successfully. The scheduler one-pass
  service exited with `0/SUCCESS`, but the latest evidence had no candidates,
  so task 4.6 end-to-end handoff remains pending.

## Runtime Env

The node-22 env file `/scratch/frd_muziyao/NWM/infra/env/compute.host.env`
was updated with:

```text
OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
OBJECT_STORE_PREFIX=s3://nhms
NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true
NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT=/ghdc/data/nwm/object-store
NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX=s3://nhms
NHMS_SCHEDULER_STAGE_NFS_RAW_TO_OBJECT_STORE=true
NHMS_SCHEDULER_NFS_RAW_STAGE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
NHMS_SCHEDULER_NFS_RAW_STAGE_PREFIX=s3://nhms
```

## Compute Read Boundary

Command shape:

```bash
srun -N1 -n1 -p CPU --time=00:02:00 bash -lc '
  hostname
  test -r /ghdc/data/nwm/object-store/raw/gfs/2026062612/manifest.json \
    && echo NFS_READ_OK || echo NFS_READ_FAIL
  test -r /scratch/frd_muziyao/nhms-prod/object-store/raw/gfs/2026062612/manifest.json \
    && echo SCRATCH_READ_OK || echo SCRATCH_READ_FAIL
'
```

Output:

```text
cn11
NFS_READ_FAIL
SCRATCH_READ_OK
```

## Raw Staging Smoke

GFS `2026-06-26T12:00:00Z`:

```text
readiness.status=ready
readiness.manifest_uri=s3://nhms/raw/gfs/2026062612/manifest.json
readiness.entry_count=397
readiness.physical_file_count=57
readiness.object_store_root=/ghdc/data/nwm/object-store
staging.status=staged
staging.manifest_key=raw/gfs/2026062612/manifest.json
staging.staged_file_count=57
staging.staged_raw_bytes=47690539
staging.target_object_store_root=/scratch/frd_muziyao/nhms-prod/object-store
```

IFS `2026-06-26T12:00:00Z`:

```text
readiness.status=ready
readiness.manifest_uri=s3://nhms/raw/IFS/2026062612/manifest.json
readiness.entry_count=424
readiness.physical_file_count=53
readiness.object_store_root=/ghdc/data/nwm/object-store
staging.status=staged
staging.manifest_key=raw/IFS/2026062612/manifest.json
staging.staged_file_count=53
staging.staged_raw_bytes=52600582
staging.target_object_store_root=/scratch/frd_muziyao/nhms-prod/object-store
```

## Services

```text
nhms-compute-api.service=active
nhms-compute-scheduler.service=inactive
last scheduler ExecStart status=0/SUCCESS
last scheduler started=2026-06-27 21:02:31 CST
last scheduler finished=2026-06-27 21:04:41 CST
```

Latest scheduler evidence:

```text
path=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/scheduler_2026062713_b817e8f6eba0.json
status=planned
execution_boundary=planning_only
candidate_count=0
source_cycle_count=0
submitted_count=0
blocked_candidate_count=0
model_run_evidence_count=0
```

Current Slurm queue at capture time:

```text
JOBID=9594_0
PARTITION=CPU
NAME=nhms_forcing
STATE=R
NODE=cn11
```
