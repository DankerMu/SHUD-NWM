# Node-22 DB-Free Scheduler Live Proof

Captured: 2026-06-28

Scope: `node22-db-free-scheduler-state` tasks 6.4 and 6.5, GitHub issue
`#836`.

## Summary

- node-22 repository was on branch `feat/issue-836-db-free-live-proof` at
  `85b8677`.
- The user scheduler unit was frozen before the bounded proof:
  `nhms-compute-scheduler.timer=disabled/inactive`.
- The scheduler unit now uses
  `/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env`.
- The scheduler env had no `DATABASE_URL` or `PIPELINE_DATABASE_URL`.
- The historical PostgreSQL listener on `:55433` remained online for rollback
  evidence, but it was not present in the scheduler env.
- A bounded DB-free pass selected both GFS and IFS
  `2026-06-26T12:00:00Z` candidates with `lock_type=file`,
  `database_url_configured=false`, node-27 NFS raw manifests, and
  `restart_stage=convert`.
- The bounded submit wrote file-journal state and submitted downstream Slurm
  jobs for both sources. Convert and forcing completed; forecast jobs were
  submitted and failed with Slurm `NODE_FAILURE`, which is outside this issue's
  DB-free scheduler acceptance scope.

## Evidence Root

```text
node-22 repo=/scratch/frd_muziyao/NWM
evidence_root=.agent-evidence/issue-836/20260628T205255Z
static_report=artifacts/issue-836/static-compose-env-check-node22.json
```

Important evidence files:

```text
dbfree-env-db-url-check.txt
dbfree-systemctl-cat-scheduler-service.txt
dbfree-index-publish-receipt.json
dbfree-readiness-republish-adapter-identity-receipt.json
dbfree-bounded-plan-output-final.json
dbfree-bounded-submit-cleanup-receipt.json
static-compose-env-check-node22.log
```

## Runtime State

```text
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
nhms-compute-scheduler.timer=disabled
nhms-compute-scheduler.timer active state=inactive
DATABASE_URL in scheduler env=absent
PIPELINE_DATABASE_URL in scheduler env=absent
:55433 listener=present
```

The static compute/display compose check was run with a clean process
environment so production env overrides could not mask template drift:

```text
status=PASS
report=/scratch/frd_muziyao/NWM/artifacts/issue-836/static-compose-env-check-node22.json
```

## DB-Free Indexes

The DB-free file providers were published under the shared object-store control
paths:

```text
registry=/ghdc/data/nwm/object-store/scheduler/registry/manifest-last.json
canonical_readiness=/ghdc/data/nwm/object-store/scheduler/canonical-readiness/index-last.json
state_index=/ghdc/data/nwm/object-store/scheduler/state-index/index-last.json
registry_model_count=1
readiness_entry_count=2
state_entry_count=2
state_warm_start_statuses=gfs:ready, IFS:ready
```

The final readiness index was regenerated from the current node-22 adapters'
unsanitized source identities. Earlier attempts using sanitized evidence
identity intentionally blocked IFS with
`canonical_readiness_index_identity_mismatch`, proving the file provider's
identity guard was active.

## Bounded Plan

Source file:

```text
.agent-evidence/issue-836/20260628T205255Z/dbfree-bounded-plan-output-final.json
```

Summary:

```text
pass_id=scheduler_2026062704_84475f553e61
status=planned
source_cycle_count=2
candidate_count=2
blocked_candidate_count=0
skipped_candidate_count=0
submitted_count=0
database_url_configured=false
scheduler_db_free_required=true
lock_type=file
```

GFS candidate:

```text
source_id=gfs
cycle_time=2026-06-26T12:00:00Z
status=selected
canonical_row_count=0
nfs_raw_manifest.status=ready
nfs_raw_manifest.source=node27_nfs_raw_manifest
nfs_raw_manifest.manifest_key=raw/gfs/2026062612/manifest.json
nfs_raw_manifest.entry_count=397
nfs_raw_manifest.physical_file_count=57
fresh_ingestion.mode=reuse_raw_then_convert
restart_stage=convert
```

IFS candidate:

```text
source_id=IFS
cycle_time=2026-06-26T12:00:00Z
status=selected
canonical_row_count=0
nfs_raw_manifest.status=ready
nfs_raw_manifest.source=node27_nfs_raw_manifest
nfs_raw_manifest.manifest_key=raw/IFS/2026062612/manifest.json
nfs_raw_manifest.entry_count=424
nfs_raw_manifest.physical_file_count=53
fresh_ingestion.mode=reuse_raw_then_convert
restart_stage=convert
```

A literal search over the final plan evidence and file-journal records produced
no `download_source_cycle` match.

## Downstream Slurm Submission

The bounded submit wrapper continued waiting after Slurm jobs reached terminal
states, so it was terminated after file-journal and Slurm accounting evidence
was captured. The cleanup used `FileOrchestrationJournalRepository` to mark the
two unsubmitted retry placeholders as `cancelled`; already submitted jobs were
not modified.

File journal roots:

```text
/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal/journal/gfs/2026062612.jsonl
/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal/journal/IFS/2026062612.jsonl
/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal/latest/gfs/2026062612/basins_qhh_shud.json
/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal/latest/IFS/2026062612/basins_qhh_shud.json
```

Slurm accounting:

```text
9818|nhms_convert|COMPLETED|0:0|00:01:09|cn23
9819|nhms_convert|COMPLETED|0:0|00:00:40|cn08
9820_0|nhms_forcing|COMPLETED|0:0|00:00:06|cn08
9821_0|nhms_forcing|COMPLETED|0:0|00:03:43|cn23
9822_0|nhms_forecast|FAILED|1:0|00:00:00|cn08
9823_0|nhms_forecast|FAILED|1:0|00:00:01|cn08
9824_0|nhms_forecast|FAILED|1:0|00:00:01|cn23
9825_0|nhms_forecast|FAILED|1:0|00:00:01|cn23
```

File-journal stage summary:

```text
gfs convert job=9819 status=succeeded
gfs forcing job=9820 status=succeeded
gfs forecast job=9822 status=failed error_code=NODE_FAILURE
gfs forecast retry job=9823 status=failed error_code=NODE_FAILURE
gfs forecast retry_2 status=cancelled slurm_job_id=None
IFS convert job=9818 status=succeeded
IFS forcing job=9821 status=succeeded
IFS forecast job=9824 status=failed error_code=NODE_FAILURE
IFS forecast retry job=9825 status=failed error_code=NODE_FAILURE
IFS forecast retry_2 status=cancelled slurm_job_id=None
```

The forecast failures prove the chain reached downstream forecast submission;
they do not invalidate the #836 acceptance claim, which is limited to DB-free
scheduler state, node-27 raw reuse, convert-or-later execution, and downstream
Slurm submission without scheduler PostgreSQL.

## Final Node-22 State

```text
residual bounded-submit python process=none
Slurm queue for frd_muziyao=empty
nhms-compute-scheduler.timer=disabled/inactive
:55433 listener=present
```

## Limits

- node-22 did not expose `uv` in the non-login SSH PATH used for this capture,
  so remote verification used the repository virtualenv at `.venv/bin/python`.
- The bounded submit result JSON was not written because the wrapper was
  terminated after file-journal and Slurm accounting evidence had been captured.
  The durable evidence for submission is the file journal plus `sacct`.
- Forecast jobs failed with `NODE_FAILURE`; this receipt does not claim a
  successful SHUD forecast run.
