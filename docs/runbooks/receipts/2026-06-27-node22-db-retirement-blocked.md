# Node-22 DB Retirement Blocked Proof

Captured: 2026-06-27

Scope: `migrate-downloads-to-node27-retire-node22-db` tasks 6.1 / 7.2.

## Summary

Node-22 `:55433` must not be stopped yet. The logged-in operations user is
`frd_muziyao`, but the PostgreSQL processes observed on `:55433` are owned by
OS user `laoban`. More importantly, node-22 production scheduler runtime still
depends on that database endpoint.

## Scheduler Runtime Evidence

Node-22 repo:

```text
/scratch/frd_muziyao/NWM
branch=master
head=c134e5b
```

Post-sync verification after removing node-22 active production download
submission code:

```text
branch=master
head=24505db
nhms-compute-api.service=active
nhms-slurm-gateway.service=active
nhms-compute-scheduler.service=inactive
infra/sbatch/download_source_cycle.sbatch=removed
config/job_type_templates.yaml active download_source_cycle mapping=removed
```

Sanitized scheduler env excerpt from
`/scratch/frd_muziyao/NWM/infra/env/compute.host.env`:

```text
DATABASE_URL=postgresql://REDACTED@10.0.2.100:55433/nhms
NHMS_SCHEDULER_LOCK_BACKEND=postgres
OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true
NHMS_SCHEDULER_STAGE_NFS_RAW_TO_OBJECT_STORE=true
```

The user service command still sources that env file:

```text
ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli plan-production --submit --continuous --max-passes 1
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.host.env
```

Latest scheduler evidence at capture time:

```text
path=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/scheduler_2026062713_0c0725993d9a.json
status=planned
selected_model_count=2
candidate_count=0
submitted_count=0
```

## Live PostgreSQL Evidence

Port listener:

```text
0.0.0.0:55433 LISTEN
[::]:55433 LISTEN
```

Observed PostgreSQL sessions included scheduler-era active connections:

```text
postgres: nhms nhms 10.0.2.100(46090) idle
postgres: nhms nhms 10.0.2.100(46100) idle in transaction
```

Observed PostgreSQL process owner:

```text
OS user=laoban
```

## Verdict

`node-22 :55433` retirement is blocked until scheduler DB responsibilities are
replaced or the scheduler is proven against a DB-free state source. Do not stop
`:55433` from the `frd_muziyao` account as part of download migration cleanup.
