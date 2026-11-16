# retire-dead-config-knobs-and-aliases (#3146)

## Why

The round-3 entropy audit (2026-11-16) found three kinds of stale configuration:
- env knobs that are templated and set in production but read by nothing;
- a template name that does not match what the code reads;
- legacy env aliases that are still honoured.

A stale config name misleads operators. A legacy alias can quietly decide live behaviour.

## Corrections to the issue, from the orchestrator survey

1. **`NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE` is a legacy alias for two keys, not one.**

   | reader | primary key | node-22 live state (2026-11-16) |
   |---|---|---|
   | `file_orchestration_journal._file_lock_guard_mode()` | `NHMS_SCHEDULER_JOURNAL_LOCK_GUARD_MODE` | set to `flock`, so the legacy key is ignored |
   | `scheduler_lease._file_lock_guard_mode()` | `NHMS_SCHEDULER_LEASE_GUARD_MODE` | unset in all five env files, so the legacy `atomic` is the **live lease guard mode** |

   The legacy key is in `compute.scheduler-dbfree.env` and `compute.replay.env`. The template sets
   `NHMS_SCHEDULER_LEASE_GUARD_MODE=atomic` ("The lease uses atomic CAS").

   The issue's "rename it to the JOURNAL key, same value" would silently flip the lease from `atomic` to `flock`. The
   migration that preserves behaviour replaces the legacy line in both live files with
   `NHMS_SCHEDULER_LEASE_GUARD_MODE=atomic`. This holds for the deployed node-22 code (`b2fc70547`) and for the code
   after this change, so the env change goes first.
2. **The `infra/env/README.md:550` pointer is correct.** It names `scheduler_lease.py`'s `_file_lock_guard_mode()`,
   which does read `NHMS_SCHEDULER_LEASE_GUARD_MODE`. The two modules share a function name, which is why it looked
   wrong. No change is needed.
3. **The governance repo-root name.**
   - The env file is sourced after the wrapper computes `REPO` from `NODE27_RESOURCE_GOVERNANCE_REPO`, so setting that
     key in the env file is circular.
   - The Python reads `NODE27_GOVERNANCE_REPO_ROOT`, and the wrapper does not pass `--repo-root`.
   - The template's `NODE27_RESOURCE_GOVERNANCE_REPO_ROOT` is read by nothing.
4. **`S3_ENDPOINT_URL` and `S3_BUCKET_NAME` are not dead everywhere.** `.env.example`, `infra/docker-compose.dev.yml`
   and `db/seeds/seed_demo.py` use them for the local dev MinIO seed. They are dead only on the display deployment
   surface: `infra/compose.display.yml`, `infra/env/display.example`, `infra/README.two-node-docker.md` and the
   two-node validator.

## What

- **Lease and journal legacy alias.** Both readers stop honouring `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE`. If it is set
  while the reader's primary key is unset, the reader **fails closed** and the error names the primary key. One
  behaviour change follows from this. Today a legacy-only `flock` is accepted by the journal reader; after the change
  it is refused. No live env reaches that case, because the journal primary key is set on node-22. Silently
  ignoring it would flip the lease to `flock` for any env, such as a restored `.bak-*`, that carries only the legacy
  line. If the primary key is set, the legacy line is ignored, which is today's precedence.
- **Other aliases.**
  - Drop the `NODE27_AUTOPIPE_LOCK_PATH` fallback (`scripts/node27_autopipe_cron.sh`, `scripts/basin_retirement/envfile.py`),
    after confirming that no node-27 env file, unit `Environment=`, drop-in or crontab sets it.
  - Make `scripts/backfill_pipeline_job_provenance.py` and `services/orchestrator/chain_repository_state.py` (~:317) read
    only the repo-wide `OBJECT_STORE_ROOT`. No env file, unit or secret on either node sets `NHMS_OBJECT_STORE_ROOT`.
  - `scripts/basin_retirement/envfile.py` falls back to `AUTOPIPE_LOCK_PATH`, the name cron reads, never directly to the
    default. Tests that isolate the live lock through the alias switch to `AUTOPIPE_LOCK_PATH`, so a node-27 pytest
    never touches the live `/tmp/autopipe.cron.lock`.
- **Dead display knobs.** Remove `CORS_ALLOWED_ORIGINS` and `NHMS_ARTIFACT_BACKEND`, and the display-surface
  `S3_ENDPOINT_URL` and `S3_BUCKET_NAME`, from:
  - the display template and compose;
  - the two-node README;
  - the two-node validator display key lists. The guard lists `COMPUTE_ONLY_PATH_ENV_KEYS` and
    `DISPLAY_AUDITED_RUNTIME_ENV` are reviewed key by key, and `AWS_*` is kept.

  **KEEP** `services/production_closure/ops_validation.py` `SERVICE_CONFIGS["api"]` `CORS_ALLOWED_ORIGINS`. It is an
  ops-evidence checklist entry: its reader is the `NHMS_PRODUCTION_OPS_API_CORS_ALLOWED_ORIGINS` evidence key, and its
  template is `docs/runbooks/production-service-config.md`. It is not the display runtime knob.

  First prove there is no reader on master **and** on the deployed node-27 checkout, using case-insensitive and
  `${VAR}` checks across settings classes, shell and compose. The dev-seed path stays.
- **Governance template.** Replace the unread `NODE27_RESOURCE_GOVERNANCE_REPO_ROOT` with the key the audit actually
  reads, `NODE27_GOVERNANCE_REPO_ROOT`.
- **Docs.** Mark the retired cold-tier, backfill and archive env names as retired where docs present them as live, and
  update the README drift table and the `node22-env-hygiene` spec for the lease key now being set live.

## Ordered rollout (never break userspace)

1. **node-22 env migration**, before the code changes. At a scheduler pass boundary:
   1. stop `nhms-compute-scheduler.timer`;
   2. wait for `nhms-compute-scheduler.service` to go inactive;
   3. back both files up under `~/NWM-presync-backup-<UTC date>/infra-env/`, preserving mode (the `node22-env-hygiene`
      backup-home rule);
   4. replace the legacy line with `NHMS_SCHEDULER_LEASE_GUARD_MODE=atomic`;
   5. diff by key name;
   6. restore the timer, with a trap that restores it on any failure.

   Guard modes are not written to pass evidence, so they are observed directly before and after:
   - `env -i bash -c 'set -a; . <file>; exec /scratch/frd_muziyao/NWM/.venv/bin/python -I -c …'` prints the return
     values of both `_file_lock_guard_mode()` functions;
   - for the pass that runs after the timer is restored (`OnActiveSec=30s`), the three guard key names in
     `/proc/<MainPID>/environ` are listed, names only;
   - that pass's summary must show `failed=0`.

   `uv run` is never used on node-22. The four other units that load the dbfree env take neither the lease nor the
   journal lock, so they are not stopped. `compute.replay.env` has no consumer (no unit, script or crontab); it is
   migrated for hygiene only.
2. **Code PR**, reviewed and merged.
3. **node-27 env edits**, after merge:
   - back up `display.env`;
   - remove the four keys, which no running service reads, so no restart is needed;
   - confirm that `display.env` still sources cleanly: `env -i bash -c 'set -a; . display.env'` exits 0;
   - fix the governance env line;
   - check `/health` returns 200;
   - check that the next governance run is not `BLOCKED rc=2` and that its summary `repo_root` is `/home/nwm/NWM`.

   Redeploying the node-27 production checkout is a separate owner decision.
4. The node-22 checkout picks up the code on its next ff-only pull. The node-22 live files no longer carry the legacy
   key, so the fail-closed branch cannot fire.

## Non-goals

- The node-22 `compute.env` `DATABASE_URL`, which points at the compose e2e DB and is loaded by no unit: report it, and
  the owner decides.
- Passwords and secret values.
- `NHMS_SCHEDULER_REGISTRY_MANIFEST` vs `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST`, which are different roles.

## Triage

Issue type: tech-debt cleanup with live configuration migration.
Fixture level: expanded. The triggers:
- legacy compatibility, because env aliases are removed;
- concurrency, because the lock and lease guard selection changes;
- release ordering across two nodes;
- config.

Upstream suggested level: absent.

Blast radius:
- A wrong lease migration changes the scheduler's cross-process lease guard on node-22.
- A removed key that is still read breaks a service or the validator.
- A fail-closed branch fires if any live env still carries only the legacy key.

Selected risk packs: Config; Concurrency; Release / rollout ordering; Legacy compatibility; Documentation.

Evidence floor:
- `uv run ruff check .`;
- the issue's named tests plus the selector set, locally and in a node-27 temp worktree;
- the node-22 pass-boundary migration receipt and the next-pass lease mode `atomic` with `failed=0`;
- the node-27 env edit receipt, `/health` 200 and the governance run is not `BLOCKED rc=2` and its summary `repo_root` is `/home/nwm/NWM`;
- deployed-checkout reader proof for every removed key.
