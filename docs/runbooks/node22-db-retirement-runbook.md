# Node-22 Historical DB Retirement Runbook

Last updated: 2026-06-29

This runbook records the completed stop gate for the historical PostgreSQL
listener on node-22 `:55433`.

## Current Status

Node-22 `:55433` was archived and stopped on 2026-06-29 after DB-free scheduler
evidence passed. The authoritative receipt is
[`2026-06-29-node22-db-retirement-stop.md`](receipts/2026-06-29-node22-db-retirement-stop.md).

- Archive:
  `/ghdc/data/nwm/operator-archives/node22-postgres-55433/20260629T025421Z`.
- Evidence:
  `/scratch/frd_muziyao/NWM/.agent-evidence/issue-837/20260629T025421Z`.
- Post-stop state: Docker container `nhms-22-e2e-db` is `exited`, restart
  policy `no`, and `ss -ltnp | grep 55433` is empty.
- Active NHMS business DB remains node-27 PostgreSQL `:55432`.

## Historical Blocker

Before #836 and #837, the historical do-not-connect `:55433` rollback listener
could not be stopped while node-22 `nhms-compute-scheduler.timer` could still
run the production scheduler with:

```text
# Historical blocker evidence only; do-not-connect rollback state.
DATABASE_URL=...:55433/nhms
NHMS_SCHEDULER_LOCK_BACKEND=postgres
```

That blocker is now closed. Current scheduler stop-gate evidence shows
`DATABASE_URL` absent, DB-free selectors set to `file`, and `lock_type=file`.

## Dependencies Removed

Node-22 could stop the historical DB only after these scheduler dependencies had
DB-free replacements:

1. Scheduler mutual exclusion:
   `NHMS_SCHEDULER_LOCK_BACKEND=postgres` must become a file/NFS lock with
   live evidence showing `lock_type=file`.
2. Model discovery:
   `PsycopgModelRegistryStore.from_env()` must no longer be the default
   production scheduler registry on node-22. The replacement should read the
   approved production model inventory from versioned model manifests or another
   tracked NFS/object-store source.
3. Candidate and pipeline state:
   `PsycopgOrchestratorRepository.from_env()` currently supplies active-run,
   completed-run, candidate-state, pipeline-job, and pipeline-event semantics.
   The scheduler needs a file-backed production journal or another DB-free
   state source before `DATABASE_URL` can be removed.
4. Warm-start/state snapshot lookup:
   `StateManager.from_env()` currently creates a `PsycopgStateSnapshotRepository`.
   Strict warm-start requires a DB-free state index for the exact successor
   checkpoint.
5. Retry and permanent-failure guard:
   Retry state and stale permanent failures must be represented by the same
   DB-free production journal, with explicit migration handling for old
   `download_source_cycle` DB rows.

## Stop Gate Result

All checks below passed on node-22 before or immediately after stopping the
historical do-not-connect `:55433` rollback listener:

```text
DATABASE_URL absent from scheduler runtime env
NHMS_SCHEDULER_LOCK_BACKEND=file
post-stop scheduler evidence lock_type=file
post-stop scheduler evidence contains no database_url dependency blocker
post-stop scheduler evidence contains no download_source_cycle submission
one GFS live cycle reaches convert-or-later without PostgreSQL
one IFS live cycle reaches convert-or-later without PostgreSQL
ss -ltnp before stop still shows :55433 only as historical DB, not scheduler-owned
rollback archive exists: pg_dump + checksum + unit/env backup + owner/process notes
ss -ltnp after stop is empty for :55433
compute API and Slurm gateway health pass after stop
```

## Retirement Sequence Record

1. Created OpenSpec change `node22-db-free-scheduler-state`.
2. Froze `nhms-compute-scheduler.timer` during DB-free migration.
3. Deployed the DB-free scheduler on node-22 while keeping `:55433` online as
   rollback.
4. Captured bounded scheduler passes with no scheduler `DATABASE_URL`.
5. Observed GFS and IFS production cycles through downstream submission without
   scheduler PostgreSQL.
6. Archived the historical database with a dump, checksum, redacted env
   metadata, listener/process snapshot, and rollback command notes.
7. Stopped the Docker-owned PostgreSQL container `nhms-22-e2e-db`. Restart
   policy was already `no`, so the stopped container is not auto-restarted.
8. Verified `ss -ltnp | grep 55433` is empty and ran a bounded post-stop
   scheduler pass with `lock_type=file`.

## Rollback

Rollback is an explicit archive recovery path, not normal operations. If a
DB-free scheduler regression requires temporary rollback, restart the archived
container first and verify the listener before restoring any old scheduler env.
Do not reconnect the scheduler to `:55433` unless an operator deliberately
chooses that rollback path.

Secret-free emergency activation commands:

```bash
cd /scratch/frd_muziyao/NWM
docker start nhms-22-e2e-db
docker inspect --format 'Name={{.Name}} State={{.State.Status}} Restart={{.HostConfig.RestartPolicy.Name}} Image={{.Config.Image}}' nhms-22-e2e-db
ss -ltnp 2>/dev/null | grep 55433
```

Post-drill cleanup commands:

```bash
docker stop nhms-22-e2e-db
if docker ps --filter name=nhms-22-e2e-db --format '{{.Names}} {{.Status}}' | grep -q .; then
  echo "BLOCKED: nhms-22-e2e-db still running after rollback cleanup" >&2
  docker ps --filter name=nhms-22-e2e-db --format '{{.ID}} {{.Names}} {{.Status}} {{.Ports}}'
  exit 1
fi
if ss -ltnp 2>/dev/null | grep -q 55433; then
  echo "BLOCKED: node-22 historical PostgreSQL :55433 still listening" >&2
  ss -ltnp 2>/dev/null | grep 55433
  exit 1
fi
```
