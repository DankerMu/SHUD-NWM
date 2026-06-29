# Node-22 Historical PostgreSQL Retirement Receipt

Date: 2026-06-29
Issue: #837
OpenSpec change: `node22-db-free-scheduler-state`
Runbook: `docs/runbooks/node22-db-retirement-runbook.md`

## Verdict

Node-22 historical PostgreSQL `:55433` was archived and stopped after the
DB-free scheduler stop gate passed. The active NHMS business database remains
node-27 PostgreSQL `:55432`; node-22 `:55433` is archived rollback state only.

Evidence root on node-22:

```text
/scratch/frd_muziyao/NWM/.agent-evidence/issue-837/20260629T025421Z
```

Archive root on shared NFS:

```text
/ghdc/data/nwm/operator-archives/node22-postgres-55433/20260629T025421Z
```

Node-22 repository head used for the stop gate:

```text
461bf357c30c4e26875de0520700b04ec3f3bb53
```

## Pre-stop DB-free Live Proof

Source receipt:
[`2026-06-28-node22-dbfree-scheduler-live-proof.md`](2026-06-28-node22-dbfree-scheduler-live-proof.md).

Evidence root on node-22:

```text
/scratch/frd_muziyao/NWM/.agent-evidence/issue-836/20260628T205255Z
```

The #836 proof satisfied the live scheduler stop gate before `:55433` was
stopped:

- Scheduler env had no `DATABASE_URL` or `PIPELINE_DATABASE_URL`.
- DB-free bounded plan recorded `database_url_configured=false`,
  `scheduler_db_free_required=true`, and `lock_type=file`.
- GFS `2026-06-26T12:00:00Z` and IFS `2026-06-26T12:00:00Z` candidates were
  both selected with node-27 NFS raw manifests and `restart_stage=convert`.
- Bounded submit wrote file-journal state and submitted downstream Slurm jobs
  for both sources.
- A literal search over the final plan and file-journal records found no
  `download_source_cycle`.

## Pre-stop Attribution

Source file: `pre-stop-metadata.json`.

- Listener before stop included `0.0.0.0:55433` and `[::]:55433`.
- Docker identified the listener owner as container `nhms-22-e2e-db`
  (`timescale/timescaledb-ha:pg15-latest`), with restart policy `no` and
  published port `0.0.0.0:55433->5432/tcp`.
- The container mounted Docker volume `nhms-22-e2e-pgdata` at
  `/home/postgres/pgdata/data`.
- PostgreSQL metadata: database `nhms`, user `nhms`, server version
  `15.2 (Ubuntu 15.2-1.pgdg22.04+1)`, data directory
  `/home/postgres/pgdata/data`, size `344 GB`.
- Active session snapshot had no external `client_addr`; observed rows were
  TimescaleDB/internal sessions.
- Established `:55433` connection snapshot contained only the `ss` header.
- Scheduler timer was already `disabled` and `inactive`; the scheduler service
  used `infra/env/compute.scheduler-dbfree.env`, not the historical DB env.

Operator limitation: `sudo -n` required a password, so root-owned socket PID
metadata from `ss -ltnp` was unavailable to `frd_muziyao`. Docker inspect,
session evidence, and the port mapping identify `nhms-22-e2e-db` as the
`:55433` listener owner; no scheduler-owned runtime process was connected.

## Archive

Source files: `archive-summary.json`, `sha256sum-check.txt`,
`pg-restore-list.txt`, `compute.env.redacted`,
`compute.scheduler-dbfree.env.redacted`, `archive-permissions.txt`.

- Archive directory: `/ghdc/data/nwm/operator-archives/node22-postgres-55433/20260629T025421Z`.
- Dump form: `pg_dump --format=directory --jobs=4 --compress=1`.
- Dump directory file count: `74`.
- Archive size: `4.3G`.
- Globals backup: `globals-no-role-passwords.sql`.
- Checksum manifest: `SHA256SUMS`.
- `sha256sum -c SHA256SUMS` passed; final entries include
  `nhms-55433-pgdumpdir/toc.dat: OK`, `pg_dump.stderr.log: OK`, and
  `pg_dump.stdout.log: OK`.
- `pg_restore -l nhms-55433-pgdumpdir` completed and listed schema/default ACL
  entries through the archive tail.
- Permission evidence records owner-only archive and evidence boundaries:
  archive root and dump dir are `frd_muziyao:huser 700`; archive sensitive
  files such as `SHA256SUMS`, `globals-no-role-passwords.sql`, and
  `pg_dump.stderr.log` are `600`; evidence root is `700`; redacted env and
  summary files are `600`.
- `find` checks found no world/group-writable sensitive files and no
  world-readable archive or evidence files.

Emergency rollback activation is intentionally secret-free and must be
operator-approved. It restarts a listener on `0.0.0.0:55433`; use it only for a
deliberate archived rollback window, then stop the container again unless the
operator has explicitly accepted a temporary rollback state.

```bash
cd /scratch/frd_muziyao/NWM
docker start nhms-22-e2e-db
docker inspect --format 'Name={{.Name}} State={{.State.Status}} Restart={{.HostConfig.RestartPolicy.Name}} Image={{.Config.Image}}' nhms-22-e2e-db
ss -ltnp 2>/dev/null | grep 55433

# If a restore into a different PostgreSQL instance is required, obtain
# credentials from the owner-only secret source, then restore from:
# /ghdc/data/nwm/operator-archives/node22-postgres-55433/20260629T025421Z/nhms-55433-pgdumpdir
```

Post-drill cleanup check:

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

## Stop Evidence

Source files: `docker-stop.log`, `post-stop-health.txt`,
`post-stop-listener-and-archive-check.txt`, `post-stop-summary.json`.

- Stop command: `docker stop nhms-22-e2e-db`.
- Stop window: `2026-06-29T02:58:16Z` to `2026-06-29T02:58:23Z`.
- Post-stop Docker state:
  `Name=/nhms-22-e2e-db State=exited Restart=no Image=timescale/timescaledb-ha:pg15-latest`.
- `ss_55433_after` was empty.
- `docker_ps_after` for `nhms-22-e2e-db` was empty.
- Scheduler timer remained `disabled` and `inactive`; scheduler service was
  `inactive`.

## Post-stop Verification

Source files: `post-stop-scheduler-plan-gfs.stdout.json`,
`compute-api-health.json`, `slurm-gateway-health.json`,
`post-stop-health.txt`, `post-stop-summary.json`.

The accepted bounded scheduler check was:

```bash
.venv/bin/python -m services.orchestrator.cli plan-production \
  --plan --max-passes 1 --source gfs --max-cycles-per-source 1 \
  --lookback-hours 72
```

Result:

- `status=planned`.
- `execution_mode=dry_run`.
- `runtime_config.database_url_configured=false`.
- `runtime_config.scheduler_db_free_required=true`.
- Scheduler state, lock, registry, canonical readiness, journal, and state
  index backends were all `file`.
- `lock.lock_type=file`.
- `root_preflight.status=ready` with no blockers.
- `no_mutation_proof.slurm_submit_called=false`; every DB/table-write field was
  `false`.
- One GFS candidate was evaluated and blocked by
  `nfs_raw_manifest_manifest_not_found`; this is raw-manifest availability, not
  PostgreSQL dependency.

Service health after stop:

- Compute API: `{"status":"ok","service":"nhms-api","version":"0.1.0"}`.
- Slurm gateway `/api/v1/slurm/health`: `status=healthy`,
  `healthy=true`, backend `slurm`, Slurm `23.11.4`.
- `squeue -u frd_muziyao` returned only the header row.

The first broad default scheduler dry-run encountered an external IFS
`503 Slow Down`; it was not used as the acceptance proof. The bounded GFS
dry-run above is the stop-gate evidence for #837.
