# Node-22 Historical DB Retirement Runbook

Last updated: 2026-06-27

This runbook defines what must be true before stopping the historical
PostgreSQL listener on node-22 `:55433`.

## Current Blocker

Do not stop `:55433` while node-22 `nhms-compute-scheduler.timer` can still run
the production scheduler with:

```text
DATABASE_URL=...:55433/nhms
NHMS_SCHEDULER_LOCK_BACKEND=postgres
```

Latest live evidence also showed scheduler passes using
`lock_type=postgres_advisory`. Removing the port before replacing those
responsibilities would turn the scheduler into a broken control plane, not a
DB-free one.

## Dependencies To Remove

Node-22 can stop the historical DB only after these scheduler dependencies have
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

## Stop Gate

All checks below must pass on node-22 before stopping `:55433`:

```text
DATABASE_URL absent from scheduler runtime env
NHMS_SCHEDULER_LOCK_BACKEND=file
latest scheduler evidence lock_type=file
latest scheduler evidence contains no database_url dependency blocker
latest scheduler evidence contains no download_source_cycle submission
one GFS live cycle reaches convert-or-later without PostgreSQL
one IFS live cycle reaches convert-or-later without PostgreSQL
ss -ltnp before stop still shows :55433 only as historical DB, not scheduler-owned
rollback archive exists: pg_dump + checksum + unit/env backup + owner/process notes
```

## Retirement Sequence

1. Create a dedicated change for DB-free scheduler state.
2. Stop `nhms-compute-scheduler.timer` during migration.
3. Deploy the DB-free scheduler on node-22, leaving `:55433` still running as
   rollback.
4. Run bounded scheduler passes with no `DATABASE_URL` and capture evidence.
5. Re-enable `nhms-compute-scheduler.timer` and observe at least one GFS and one
   IFS production cycle through downstream submission without PostgreSQL.
6. Archive the historical database with a dump, checksum, unit/env metadata,
   listener/process snapshot, and rollback command notes.
7. Stop and disable the PostgreSQL service from the owning account or root
   context. The project operator account is `frd_muziyao`; previously observed
   PostgreSQL processes were owned by OS user `laoban`, so stopping may require
   that owner or an administrator.
8. Verify `ss -ltnp | grep 55433` is empty and run one final scheduler pass.

## Rollback

If DB-free scheduler evidence regresses before step 7, restore the previous
scheduler env and timer while keeping `:55433` online. If regression is found
after stopping the DB, restart the archived PostgreSQL service first, then
restore the old scheduler env.
