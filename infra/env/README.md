# Environment templates of the systemd units

The `*.example` files in this directory are the tracked templates of the env
files that the NHMS systemd user units on node-22 and node-27 load. They are not
final production credentials.

Real local `display.env` and readonly validation
`display-readonly-secrets.env` files contain production secret-bearing values.

**Slurm gateway service token contract (#1684):** the shared scheduler
credential is `SLURM_GATEWAY_SERVICE_TOKEN` and lives ONLY in untracked,
owner-mode-0600 environment sources consumed by both the gateway unit
(`EnvironmentFile=`) and the scheduler unit (drop-in `EnvironmentFile=`), in
production on node-22 at
`/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env` (mode 0600, owned by
the service account). The value is never committed, logged, serialized, passed
via argv, or published in OpenAPI/evidence; only the variable name is
checked-in.
Create them with owner-only permissions, for example
`install -m 0600 /dev/null infra/env/display-readonly-secrets.env`, or under
`umask 077`, and keep them untracked. Before sourcing any local secret-source
file, use a fail-closed guard that checks the file exists, verifies `stat -c
'%a' <file>` is `600`, prints `BLOCKED:`, and exits before `source` if the
check fails. A failed mode check blocks validation rather than producing
evidence from a readable secret file.

Required canonical published artifact variables:

- `NHMS_PUBLISHED_ARTIFACT_ROOT`: published artifact root used by the app.
- `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX`: canonical URI prefix, normally
  `published://`.
- `NHMS_PUBLISHED_ARTIFACT_S3_BUCKET`: optional allowlisted bucket for published
  artifact reads.
- `NHMS_PUBLISHED_ARTIFACT_S3_PREFIX`: optional allowlisted prefix for published
  artifact reads.

Do not use unprefixed `PUBLISHED_ARTIFACT_ROOT` as an app runtime variable.

## Readonly DB boundary validation with a local secret source

The full form of the fail-closed guard described above, as the readonly DB
boundary validation on node-27 uses it.

- `EVIDENCE_ROOT` is the evidence directory of the run: a directory named after
  the run under one of the approved evidence roots (see "All generated validation
  evidence" below), for example `artifacts/two-node-e2e/<run id>`. The validator
  is given the parent of that directory and its last component, so it writes into
  `$EVIDENCE_ROOT/db/readonly-db-boundary/` and not into a second directory of the
  same name below it.
- `NHMS_READONLY_DB_VALIDATION_RUN_ID` is the business run under validation
  (`hydro.hydro_run.run_id`), not the evidence bundle. To name the bundle through
  the environment use `NHMS_READONLY_DB_VALIDATION_EVIDENCE_RUN_ID`.
- The four per-source exports inside the loop are optional overrides: without
  them the validator discovers the latest display-ready run of the source itself
  (#2484).
- Without a real readonly database URL the lane reports `BLOCKED`, never `PASS`.

```bash
: "${EVIDENCE_ROOT:?export EVIDENCE_ROOT first}"
EVIDENCE_PARENT="$(dirname "$EVIDENCE_ROOT")"
EVIDENCE_RUN_ID="$(basename "$EVIDENCE_ROOT")"
export NHMS_SERVICE_ROLE=display_readonly
export NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true
export NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false

# Load the real readonly DSN in a secret-safe way; the env file is untracked and mode 0600.
# Create the local secret-source file once; do not run install on an existing file, it truncates.
READONLY_SECRET_SOURCE=infra/env/display-readonly-secrets.env
if [ ! -e "$READONLY_SECRET_SOURCE" ]; then
  install -m 0600 /dev/null "$READONLY_SECRET_SOURCE"
elif [ ! -f "$READONLY_SECRET_SOURCE" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be a regular 0600 file before sourcing" >&2
  exit 1
fi
$EDITOR "$READONLY_SECRET_SOURCE"
readonly_secret_mode="$(stat -c '%a' "$READONLY_SECRET_SOURCE")" || {
  echo "BLOCKED: cannot stat $READONLY_SECRET_SOURCE before sourcing" >&2
  exit 1
}
if [ "$readonly_secret_mode" != "600" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" >&2
  exit 1
fi
set -a
. "$READONLY_SECRET_SOURCE"
set +a
# Or read it from the site secret manager:
# export NHMS_DISPLAY_READONLY_DATABASE_URL="$(secret-manager read nhms/display/readonly-db-url)"

for SOURCE in GFS IFS; do
  export NHMS_READONLY_DB_VALIDATION_SOURCE="$SOURCE"
  export NHMS_READONLY_DB_VALIDATION_CYCLE_TIME='<cycle_time-for-source>'
  export NHMS_READONLY_DB_VALIDATION_RUN_ID='<business-run-id-for-source>'
  export NHMS_READONLY_DB_VALIDATION_MODEL_ID=basins_qhh_shud
  export NHMS_READONLY_DB_VALIDATION_JOB_ID='<job-id-with-published-log-for-source>'

  uv run python scripts/validate_readonly_db_boundary.py \
    --evidence-root "$EVIDENCE_PARENT" \
    --run-id "$EVIDENCE_RUN_ID-db-$SOURCE" \
    --force
done

uv run python scripts/validate_readonly_db_boundary.py \
  --evidence-root "$EVIDENCE_PARENT" \
  --run-id "$EVIDENCE_RUN_ID" \
  --merge-declared-source GFS \
  --merge-declared-source IFS \
  --merge-source-dir "$EVIDENCE_PARENT/$EVIDENCE_RUN_ID-db-GFS/db/readonly-db-boundary" \
  --merge-source-dir "$EVIDENCE_PARENT/$EVIDENCE_RUN_ID-db-IFS/db/readonly-db-boundary" \
  --force
```

## node-22 unit → EnvironmentFile authority (#1694)

Which file a node-22 user systemd unit actually loads. Paths are on node-22
under `/scratch/frd_muziyao/`. The rows of `nhms-compute-scheduler.service`,
`nhms-scheduler-evidence-retention.service`,
`nhms-scheduler-file-provider-refresh.service` and `nhms-slurm-gateway.service`
were verified read-only on the live host 2026-09-02; the fifth unit of that
reading, `nhms-compute-api.service`, is retired (#3394) and has no row. The
`nhms-node22-unit-failure-alert@.service` row was
deployed 2026-11-12 (issue 3127; the env file sends through the SMTPS wrapper
`scripts/node22_smtp_sendmail.sh`), receipt
`docs/runbooks/receipts/2026-11-12-issue-3127-node22-unit-failure-alert-deploy.md`.

| Unit (`systemctl --user`) | EnvironmentFile(s) loaded | Tracked template |
|---|---|---|
| `nhms-compute-scheduler.service` | `NWM/infra/env/compute.scheduler-dbfree.env` + `nhms-prod/secrets/slurm-gateway.env` | `compute.scheduler-dbfree.env.example`; the secret file has none by design (credential-bearing, mode 0600, see #1684 above) |
| `nhms-scheduler-evidence-retention.service` | `NWM/infra/env/compute.scheduler-dbfree.env` | `compute.scheduler-dbfree.env.example` |
| `nhms-scheduler-file-provider-refresh.service` | `NWM/infra/env/compute.scheduler-provider-refresh.env` | `compute.scheduler-provider-refresh.env.example` |
| `nhms-slurm-gateway.service` | `NWM/infra/env/compute.host.env` + `nhms-prod/secrets/slurm-gateway.env` | **untracked, no template** for `compute.host.env` |
| `nhms-node22-unit-failure-alert@.service` | `NWM/infra/env/node22-unit-failure-alert.env` | `node22-unit-failure-alert.example` |

Notes:

- **Two of these units reach their file set through a drop-in override, not the
  tracked unit body.** Both drop-ins emit an empty `EnvironmentFile=` first
  (which resets the inherited list) and then re-add the files listed above:
  - `nhms-compute-scheduler.service` — re-adds `compute.scheduler-dbfree.env`
    plus the untracked secret.
  - `nhms-slurm-gateway.service` — re-adds `compute.host.env` first, then the
    same untracked secret. The tracked template
    `infra/systemd/nhms-slurm-gateway.service` is the *generic* unit and points
    at a different path (`/opt/SHUD-NWM/infra/env/slurm-gateway.secret`); its
    own header (`:3-13`) says so explicitly.

  Reading only `infra/systemd/*.service` in this repo will therefore give you
  the wrong answer for both units. `systemctl --user show <unit> -p
  EnvironmentFiles` is the authority; the drop-in procedure is
  `docs/runbooks/current-production-ops.md` §3.2.2.
- **`infra/env/compute.env`, where it is still present on the host, is a
  leftover of the docker/compose lane retired by #3231**: no unit reads it and
  it has no tracked template. Verified on node-22 read-only 2026-09-02, receipt
  `.workplans/pr-1956/node22-compute-env-receipt.log`: no user systemd unit
  lists it in `EnvironmentFiles` (receipt `:1-13`: the four units verified on
  2026-09-02 in the table above and the fifth unit of that reading,
  `nhms-compute-api.service`, retired by #3394).
  Values in it — including basin roots and scheduler model/basin filters — are
  **not** the production scheduler's configuration and must not be quoted as
  such.
  The `DATABASE_URL` it still carries on node-22 is the same superseded value:
  no node-22 unit loads this file, so the key reaches no running process and
  does not breach the DB-free gate. It is kept, not scrubbed, by owner decision
  (#3164); do not copy it into any env file a unit does load.
- `compute.host.env` is untracked with no committed template. That gap is
  recorded, not fixed, by #1694.
- The scheduler's lag and lookback keys (`NHMS_SCHEDULER_CYCLE_LAG_HOURS`,
  `NHMS_SCHEDULER_LOOKBACK_HOURS`) in `compute.host.env` and `compute.replay.env`
  are inert. Read on the compute control host on 2026-11-25: both files still carry
  `NHMS_SCHEDULER_CYCLE_LAG_HOURS=16` (lookback 48 and 288), from before the
  scheduler's own file moved to 10 / 32 (#3225). The scheduler service loads
  `compute.scheduler-dbfree.env` plus the gateway secret file (table above);
  `compute.host.env` is loaded by the gateway, which reads neither key (it does
  read other `NHMS_SCHEDULER_*` keys from it, so drop only these two). The stale
  values change nothing; drop those two keys at
  the next edit of those two host files, and read the scheduler's lag and
  lookback from `compute.scheduler-dbfree.env` (`#3233`).
- 部署的单元体与 drop-in 的逐字节副本在 `infra/systemd/node22/`（`MANIFEST.json`，收据 `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md` 与 `docs/runbooks/receipts/2026-11-26-w3-gateway-runtime-path-dropin-snapshot.md`），本表仍是 EnvironmentFile 权威，`tests/test_node22_unit_tracking.py` 以本表校验那些文件的 `EnvironmentFile=`

### Where the scheduler's basin configuration actually lives

`NHMS_BASINS_ROOT`, `NHMS_SCHEDULER_BASIN_IDS`, and `NHMS_SCHEDULER_MODEL_IDS`
for the production scheduler are whatever `compute.scheduler-dbfree.env` says
(template `compute.scheduler-dbfree.env.example`) — never `compute.env`.

- Live scheduler value as of 2026-09-02: `NHMS_BASINS_ROOT=/volume/nwm/Basins`
  (exists on the node-22 login host and on compute node cn01, 2026-09-02),
  with both `NHMS_SCHEDULER_BASIN_IDS` and `NHMS_SCHEDULER_MODEL_IDS` empty
  (registry-driven selection, as required above).
- `/ghdc/data/nwm/Basins` also exists on the node-22 **login host**
  (receipt `.workplans/pr-1956/node22-compute-env-receipt.log` `:107-116`, read-only
  2026-09-02) and its contents differ from the scheduler root: the same receipt
  records 44 top-level entries under `/volume/nwm/Basins` against 33 under
  `/ghdc/data/nwm/Basins`, with 17 entries only in the former (`CJ-BYH`,
  `Pearl_*`, …) and 6 only in the latter (`BYH DTH_XJ DTH_YJ HJ MJ WJ`). It is
  the **separate shared-NFS basin root that node-27 ingest reads** (as
  `/home/ghdc/nwm/Basins`). It is not the scheduler root, and it is **not
  mounted on the compute nodes** (cn01 check 2026-09-02: MISSING, receipt
  `:94-97`) — do not treat either path as "the" basin root without naming the
  lane.

Compute role, node 22:

Which file a live node-22 unit loads is stated by the unit → EnvironmentFile
table above.

- `OBJECT_STORE_ROOT` is the compute-visible staging root;
  `NHMS_OBJECT_STORE_COPYBACK_ROOT` is the shared object-store mirror used
  after publish.
- The production scheduler is DB-free. It must set every scheduler backend
  selector to `file` and the registry/readiness/journal/state-index path
  variables. DB-free is the scheduler's only mode (#3255), so
  `NHMS_SCHEDULER_DB_FREE_REQUIRED` no longer switches it; the templates keep
  `NHMS_SCHEDULER_DB_FREE_REQUIRED=true` as the job-env contract (every run
  manifest records `scheduler_db_free_required: "true"` and the gateway exports
  the DB-free worker switches into the Slurm job env). On the
  live node-22 units those variables come from `compute.scheduler-dbfree.env`
  (tracked template `compute.scheduler-dbfree.env.example`), per the table
  above.
  The live file is untracked, so the template is *not* evidence for its
  contents. Read on node-22 2026-09-10 (`-rw-------` `frd_muziyao:huser`):
  `:13 OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store`,
  `:15 NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store` — both
  present, which is what the canonical-precip backfill recovery command in
  `docs/runbooks/current-production-ops.md` §5.3 depends on. Same scan: only
  `compute.host.env`, `compute.replay.env` and `compute.scheduler-dbfree.env`
  carry `NHMS_OBJECT_STORE_COPYBACK_ROOT`.
  `DATABASE_URL` belongs to an explicit archived rollback drill. Node-22
  `:55433` is stopped/archived and is not scheduler runtime env.
- The node-22 DB-free scheduler live env
  (`compute.scheduler-dbfree.env`, referenced by systemd `EnvironmentFile=`)
  has exactly one tracked source: `compute.scheduler-dbfree.env.example`.
  Its required key set is the union of two layers, both carried by that
  single template:
  - scheduler keys: lock/state/registry/journal/evidence/allow-list and
    business-scope variables (`NHMS_SCHEDULER_*`);
  - pipeline runtime roots: `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`,
    `OBJECT_STORE_PREFIX`, `NHMS_BASINS_ROOT` — dereferenced by the runbook
    provision/publisher shell and required by `plan-production` preflight;
    the two local roots must stay inside `NHMS_SCHEDULER_ALLOWED_ROOTS`.
- Object-store copyback mutual exclusion (#2035). Every writer that promotes a
  directory tree under `NHMS_OBJECT_STORE_COPYBACK_ROOT` — the publisher's
  q_down, run-products and canonical-precipitation lanes, the orchestrator's
  run-tree copyback, and both backfill CLIs — first takes an exclusive `flock`
  on the fixed path `$NHMS_OBJECT_STORE_COPYBACK_ROOT/.nhms-copyback-batch.lock`.
  - The lock path is **fixed and has no env override**: it is anchored under the
    copyback root because that is the one path every writer has already resolved,
    so a private `/tmp` (systemd `PrivateTmp=true`, Slurm `job_container/tmpfs`)
    cannot split one mutex into two inodes. The file is created `0o600` and is
    never unlinked by the code — both enforced directly (`_LOCK_MODE` in
    `packages/common/copyback_guard.py`; no unlink call anywhere in the module).
    "Owned by the writer itself" is *not* separately enforced: the code asserts
    `lock owner == copyback root owner` and `current euid == lock owner`, and the
    two coincide only because every configured writer is the same uid and a
    foreign uid is refused before the file exists.
  - A killed holder's lock is released by the **local** kernel at process exit,
    so a stale file is not a stale lock. The production root is not local:
    `/ghdc/data/nwm/object-store` on node-22 is an NFSv4.2 mount of
    `ghdc:/home/ghdc` (measured 2026-09-10). A process death still releases at
    exit; a **host** death does not — the server holds the lock until that
    client's lease expires.
  - `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS` (optional, **default 900**)
    bounds how long a writer waits. Contention waits rather than refuses;
    exceeding the deadline raises a distinct loud error and never falls back to
    an unlocked promote. Unset or empty means 900 s; a non-numeric or
    non-positive value is a hard configuration refusal, not a silent default.
    900 s is sized against the measured hold (~2.2 GB per acquisition at
    ~62 MB/s NFS throughput is ~36 s); how often a lane acquires and how that
    sizes the deadline is stated once, beside
    `DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS` in
    `packages/common/copyback_guard.py`. That arithmetic is deliberately
    conservative and is left unretuned.
  - All copyback writers must run as one uid (`frd_muziyao` on node-22): the
    `0o600` mode plus the ownership assertions make a writer under another
    account fail closed instead of running unlocked. The lock file's owner is
    compared both to the current euid **and** to the copyback root's owner, and
    a foreign uid is refused before it can create the file, so the lock cannot be
    poisoned from either direction. Exclusion is node-local — see the non-goals in
    `openspec/changes/harden-copyback-batch-mutex-and-dir-traversal/proposal.md`.
  - **`NHMS_OBJECT_STORE_COPYBACK_ROOT` MUST be owned by the single writer uid**
    — the account the publisher, orchestrator and both backfill CLIs run as. The
    requirement is *equality of uid*, not "the writer can write the root": a
    group-writable root owned by another account (a container uid in a
    supplementary group, for example) is **refused, not shared**, because the
    lock file's owner is anchored to the root's owner. Check with
    `stat -c '%u %a %n' "$NHMS_OBJECT_STORE_COPYBACK_ROOT"` and compare against
    the writer's `id -u`; a mismatch fails every copyback closed. The message
    has two shapes, and neither is a timeout:
    - **no lock file yet** and the writer is not the root's owner → refused
      before the file is created, and the message names both uids and the lock
      path;
    - **lock file already there and owned by someone else** → `cannot acquire
      copyback batch lock <path>: [Errno 13] Permission denied`, path only, no
      uid. That `Permission denied` *is* the foreign-owner signal; the recovery
      steps below (`ls -ln <lock>`, `stat -c '%u' <root>`) are what name the
      owner.
  - **Recovering a stuck lock file.** Three shapes, and only one of them is
    touchable:
    - a **live holder** — some process still holds the fd
      (`lsof "$NHMS_OBJECT_STORE_COPYBACK_ROOT/.nhms-copyback-batch.lock"` or
      `fuser -v <path>` prints a pid) → **do not touch it**; wait, or find out why
      that writer is stuck.
    - **owner correct, no local holder, writers still timing out** — only
      possible on the NFS root: the holder's whole host died and the lock stands
      until the server expires that client's lease. **Also do not touch it** —
      unlinking splits the mutex onto a fresh inode exactly as it would with a
      live holder. Wait out the lease, or go establish what happened to that
      host. This is *not* an orphan; the next bullet does not apply.
    - an **orphan owned by the wrong uid** — `ls -ln <path>` shows an owner
      different from `stat -c '%u' "$NHMS_OBJECT_STORE_COPYBACK_ROOT"` and no
      process holds it → repair it, otherwise every writer fails closed forever:
      `sudo chown "$(stat -c '%u:%g' "$NHMS_OBJECT_STORE_COPYBACK_ROOT")" <path>`
      works unconditionally; `rm -f <path>` also works, and does **not** require
      the lock file's owner — POSIX takes unlink permission from write+execute
      on the *directory*, provided no sticky bit is set. Measured 2026-09-10 on
      node-22: `/ghdc` 755 root:root, `/ghdc/data` 777 root:root,
      `/ghdc/data/nwm` and `/ghdc/data/nwm/object-store` both 775
      `frd_muziyao`(1103):`huser`(1078) — no sticky bit anywhere on the chain,
      so any member of gid 1078 can remove it. If `ls -ld` ever shows a `t` on
      that chain, fall back to `sudo chown`.
  - `services/orchestrator/retention.py` descends only `root/<prefix>` and
    `root/runs`, never root-level files, so the lock file is invisible to it.
- The DB-free scheduler's trusted raw authority is the canonical shared-NFS
  node-22 topology path. Runtime preflight requires both
  `NHMS_OBJECT_STORE_COPYBACK_ROOT` and
  `NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT` to resolve to that same fixed,
  allow-listed, readable directory and to each other. Equality between the two
  mutable variables does not establish authority; an arbitrary allow-listed
  staging root is not authority.
- Node-22 compute-control must set
  `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc` and
  `NHMS_REQUIRE_FORECAST_WARM_START=true`. That makes the scheduler run SHUD and
  DB-free `state_save_qc` so forecast warm-start state continues to advance, then
  stop before parse/publish. Node-27 data-plane ingest owns parse, QC, DB writes,
  and display publication from those forecast outputs. For an all-basin
  backfill bootstrap, set `NHMS_FORECAST_WARM_START_REQUIRED_FROM` to the first
  cycle that must be warm-started; earlier cycles may cold-start only to seed
  DB-free state for that boundary.

#### Required keys for the node-22 DB-free scheduler lane (machine-readable)

The prose above and in the bullets before it states this lane's required set as
"the union of two layers" — wording that never says "must set" for most of it,
and that vagueness is what produced issue #2075 (a rebuilt node missing
`NHMS_ORCHESTRATOR_TERMINAL_STAGE` runs the chain into `publish` and fails
`DATABASE_URL_MISSING` every cycle). The authority is therefore the block
below, not the prose. `tests/test_env_templates.py` parses it and asserts
`compute.scheduler-dbfree.env.example` satisfies every entry — key present, and
**value equal** where a value is pinned. Add a key here and the guard enforces
it on the next test run; do not restate a mandate in prose alone.

<!-- nhms-required-keys: compute.scheduler-dbfree -->

```text
# Required environment keys for infra/env/compute.scheduler-dbfree.env,
# whose single tracked source is compute.scheduler-dbfree.env.example.
#
#   KEY=value   the key must be present AND carry exactly this value
#   KEY         the key must be present; the value is site-specific
#
# DB-free job-env contract and file-backed selectors: every scheduler backend
# selector is pinned to `file`, and NHMS_SCHEDULER_DB_FREE_REQUIRED to `true`
# as the job-env contract (it no longer selects the scheduler mode, #3255).
NHMS_SCHEDULER_DB_FREE_REQUIRED=true
NHMS_SCHEDULER_LOCK_BACKEND=file
NHMS_SCHEDULER_STATE_BACKEND=file
NHMS_SCHEDULER_REGISTRY_BACKEND=file
NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND=file
NHMS_SCHEDULER_JOURNAL_BACKEND=file
NHMS_SCHEDULER_STATE_INDEX_BACKEND=file

# Provider path variables (site-specific absolute paths).
NHMS_SCHEDULER_REGISTRY_MANIFEST
NHMS_SCHEDULER_CANONICAL_READINESS_INDEX
NHMS_SCHEDULER_JOURNAL_ROOT
NHMS_SCHEDULER_STATE_INDEX
# Private state index for Slurm state_save_qc; without it the job falls back
# to NHMS_SCHEDULER_STATE_INDEX and rewrites the shared lane (#3123).
NHMS_SLURM_SCHEDULER_STATE_INDEX
# Private registry manifest (worker mirror) for Slurm jobs; without it the job
# falls back to NHMS_SCHEDULER_REGISTRY_MANIFEST. The manifest context requires
# its generation to match the shared manifest (#3171).
NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST
# Deliberately NOT required: NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX
# stays unset. No producer maintains a private readiness index (provider
# refresh publishes only the shared one) and no Slurm entry point of the
# node-22 chain reads it; unset, jobs fall back to the shared
# NHMS_SCHEDULER_CANONICAL_READINESS_INDEX that refresh keeps current (#3171).

# Scheduler no-flag business validation requires these roots and the
# approved-root policy.
NHMS_SCHEDULER_LOCK_ROOT
NHMS_SCHEDULER_EVIDENCE_ROOT
NHMS_SCHEDULER_RUNTIME_ROOT
NHMS_SCHEDULER_TEMP_ROOT
NHMS_SCHEDULER_ALLOWED_ROOTS
NHMS_SCHEDULER_SOURCES

# Trusted shared-NFS raw-manifest authority: runtime preflight requires both
# to resolve to the same fixed node-22 topology path.
NHMS_OBJECT_STORE_COPYBACK_ROOT
NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT

# Pipeline runtime roots dereferenced by the runbook provision/publisher shell
# and required by `plan-production` preflight.
WORKSPACE_ROOT
OBJECT_STORE_ROOT
OBJECT_STORE_PREFIX
NHMS_BASINS_ROOT

# Canonical registry authority: the consumer environment stays `true`; only a
# one-off baseline staging command sets it false.
NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true

# Orchestrator chain terminal stage (#2075). node-22 is DB-free, so the chain
# must stop before parse/publish. `forecast` is NOT an acceptable value.
NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc
NHMS_REQUIRE_FORECAST_WARM_START=true

# Slurm gateway backend (#2543). The scheduler reads the same selector as the
# gateway: only `slurm` makes it split cohorts by the gateway's
# config/resource_profiles.yaml override keys (the default `mock` never does).
SLURM_GATEWAY_BACKEND=slurm
```

<!-- /nhms-required-keys -->
- Production scheduler model selection is registry-driven. Keep
  `NHMS_SCHEDULER_MODEL_IDS` and `NHMS_SCHEDULER_BASIN_IDS` empty for normal
  operations; publish the full Basins file registry with
  `scripts/publish_scheduler_file_registry.py` instead of hand-maintaining a
  qhh-only manifest.
- Scheduler no-flag business validation requires `NHMS_SCHEDULER_LOCK_ROOT`,
  `NHMS_SCHEDULER_EVIDENCE_ROOT`, `NHMS_SCHEDULER_RUNTIME_ROOT`,
  `NHMS_SCHEDULER_TEMP_ROOT`, non-empty `NHMS_SCHEDULER_ALLOWED_ROOTS`, and
  source/model filter envs such as `NHMS_SCHEDULER_SOURCES`,
  `NHMS_SCHEDULER_MODEL_IDS`, `NHMS_SCHEDULER_BASIN_IDS`,
  `NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE`, `NHMS_SCHEDULER_INTERVAL_SECONDS`,
  and `NHMS_SCHEDULER_MAX_PASSES`. `NHMS_SCHEDULER_ALLOWED_ROOTS` is an
  independent approved-root policy separated by `:`; do not derive it from the
  candidate runtime roots at execution time. `WORKSPACE_ROOT`,
  `OBJECT_STORE_ROOT`, `NHMS_PUBLISHED_ARTIFACT_ROOT`,
  `NHMS_SCHEDULER_RUNTIME_ROOT`, and `NHMS_SCHEDULER_TEMP_ROOT` must be under
  one of those approved roots. Lock and evidence roots must be under
  `WORKSPACE_ROOT`; explicit
  `--workspace-root`, `--lock-path`, and `--evidence-dir` are diagnostic
  compatibility, not the scheduler's proof path.
- Allowed only on compute: Slurm gateway settings, writable workspace,
  Basins/model asset paths, and `SHUD_EXECUTABLE`.

Display role, node 27:

- Required: `NHMS_SERVICE_ROLE=display_readonly`,
  `NHMS_REQUIRE_SERVICE_ROLE=true`, `NHMS_AUTH_MODE=production`,
  `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true`,
  `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false`, readonly `DATABASE_URL`, and a
  readonly `OBJECT_STORE_ROOT`.
- Keys the display role refuses at startup (owner: `apps/api/runtime_mode.py`):
  `SLURM_GATEWAY_URL`, `SLURM_GATEWAY_BACKEND`, `SLURM_GATEWAY_TEMPLATE_DIR`,
  `SLURM_GATEWAY_WORKSPACE_DIR`, `WORKSPACE_ROOT`, `RUN_WORKSPACE_ROOT`,
  `SHARED_LOG_ROOT`, `NHMS_OBJECT_STORE_COPYBACK_ROOT`,
  `NHMS_SCHEDULER_LOCK_ROOT`, `NHMS_SCHEDULER_EVIDENCE_ROOT`,
  `NHMS_SCHEDULER_RUNTIME_ROOT`, `NHMS_SCHEDULER_TEMP_ROOT`,
  `NHMS_BASINS_ROOT`, `NHMS_MODEL_ASSET_ROOT`, `SHUD_EXECUTABLE`,
  `MUNGE_SOCKET`, `MUNGE_KEY`, and `DOCKER_HOST`.

All generated validation evidence must stay under `artifacts/` in this
repository or under `/scratch/frd_muziyao` outside this checkout. Do not write
reports or evidence into `infra/env/`; real local env files such as
`compute.env`, `display.env`, `display-readonly-secrets.env`, and `local.env`
are intentionally ignored while `*.example` and this README remain trackable.
`infra/docker-compose.dev.yml` remains a local development dependency stack,
not a production deployment.

Node-27 ingest role:

- `infra/env/node27-ingest.example` is the committed template for
  `scripts/node27_autopipe_cron.sh` and
  `infra/systemd/nhms-node27-autopipe.{service,timer}`. Copy it to an untracked
  `infra/env/node27-ingest.env` with mode `0600` and writer-capable
  `DATABASE_URL` on node-27.
- This env is not a display API env. It uses
  `NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest` and must not set
  `NHMS_SERVICE_ROLE=display_readonly` or use a display/readonly DB user.
- Required ingest keys are `DATABASE_URL`, `OBJECT_STORE_ROOT`, `BASINS_ROOT`,
  `AUTOPIPE_WORK_ROOT`, and `AUTOPIPE_LOG_ROOT`. The cron wrapper blocks before
  Python ingest and coverage backstop when the ingest env is missing or unsafe.
- `NODE27_INGEST_ALLOWED_DATABASE_ENDPOINTS` defaults to
  `127.0.0.1:55432,localhost:55432`; set it only when node-27 ingest must use a
  different local node-27 PostgreSQL endpoint.
- Current node-27 autopipeline does not call a node-22 DB rollback mirror and
  treats `N22_DSN`, `NHMS_NODE22_DSN_SOURCE`, and
  `NHMS_ALLOW_ARCHIVED_NODE22_DB_ROLLBACK_MIRROR` as forbidden runtime env.
  Every run must provide an object-store
  `runs/<run_id>/input/forcing_domain_handoff.json`; missing handoff is reported
  as `OBJECT_STORE_FORCING_HANDOFF_REQUIRED`.
- `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` ships commented out (#2504). Set
  it only to the value `infra/env/node27-timeseries-retention.env` carries: it
  lets the display coverage refresh lower populated rows whose valid-time end
  is older than the display watermark minus that window. Unset = no relaxation.
- `AUTOPIPE_MAX_CYCLE_AGE_DAYS` ships as `14` (#3062): the tick drops runs whose
  cycle is older than now UTC minus that many days (truncated to the hour) and
  counts them as `skipped_older_than_floor`. Unset/empty = no floor; a value
  that is not a positive integer blocks the tick at preflight. Older cycles are
  ingested manually with `--only-cycle YYYYMMDDHH`, which bypasses the floor.

Node-27 download role:

- `infra/env/node27-download.example` is the committed template for
  `scripts/node27_download_once.sh` and the template units
  `infra/systemd/nhms-node27-download@.{service,timer}`, deployed as one
  instance per source (`@GFS`, `@IFS`). Copy it to untracked
  `infra/env/node27-download.env` with mode `0600` and writer-capable
  `DATABASE_URL` on node-27.
- Leave `NODE27_DOWNLOAD_CYCLE_TIME` empty for production automation. The runner
  scans the allowed cycles from the start of the raw chain (the oldest cycle
  with a raw manifest in the continuity lookback window) up to the latest
  allowed UTC cycle from `NHMS_NODE27_DOWNLOAD_ALLOWED_CYCLE_HOURS_UTC` after
  the delay. The delay is per source:
  `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_<SOURCE>` when set, otherwise the shared
  `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS` (template: 6 hours for GFS, 8 hours
  shared); a pass that runs several sources uses the largest of their delays.
  A cycle
  counts as downloaded only when its raw files are complete for every selected
  source: the manifest matches and every file it lists is a non-empty regular
  file, the rule the scheduler applies. A manifest alone does not count. The
  latest allowed cycle is served first when it is incomplete; otherwise the
  oldest incomplete cycle is served; otherwise the latest allowed cycle. When no
  raw seed exists in the lookback window, it selects the latest allowed cycle
  as the first seed. The pass summary lists the incomplete cycles with the
  status and reason per source under `cycle_time_selection_detail`. Set
  `NODE27_DOWNLOAD_CYCLE_TIME` only for explicit operator-directed runs.
- Each unit instance passes its source to the wrapper. The wrapper then runs
  that source only and writes `download-<source>.log` and
  `last-summary-<source>.json` next to the configured log and summary files.
  The runner locks per source (`NODE27_DOWNLOAD_LOCK_PATH` plus `.gfs` /
  `.ifs`), so the two instances do not block each other, while a manual pass
  without a source is excluded by either of them.
- To get past an older cycle the upstream no longer serves, set
  `NODE27_DOWNLOAD_CONTINUITY_START_TIME` to a cycle after it; the scan then
  starts there. This start ignores the lookback window, so unset it again once
  that cycle has left the window; left in place, cycles after it that raw
  retention later removes would be selected again.
- The download env is not display runtime config. It must use
  `NHMS_NODE27_DOWNLOAD_ROLE=node27_data_plane_download`, local node-27
  PostgreSQL `:55432`, `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`, and a
  node-27 local `WORKSPACE_ROOT`.

Node-27 parse-failure residency alert (#2529):

- `infra/env/node27-parse-failure-residency-alert.example` is the OPTIONAL
  knob template for `scripts/node27_parse_failure_residency_alert.py` and
  `infra/systemd/nhms-node27-parse-failure-residency-alert.{service,timer}`.
  Every line ships commented (the defaults are the deployed values); copy it to
  untracked `infra/env/node27-parse-failure-residency-alert.env` (mode `0600`)
  only to change one. The read-only `nhms_display_ro` `DATABASE_URL` comes from
  `infra/env/node27-frontier-alert.env`, not from this file.

Node-27 resource governance role:

- `infra/env/node27-resource-governance.example` is the committed template for
  `scripts/node27_resource_governance_once.sh` and
  `infra/systemd/nhms-node27-resource-governance.{service,timer}`. Copy it to
  untracked `infra/env/node27-resource-governance.env` with mode `0600`.
- The audit is read-only. A display/readonly DB account is enough; do not use
  this env for ingest, cleanup writes, chunk drops, or compression execution.
- The daily receipt reports filesystem pressure, node-27 service states,
  PostgreSQL/TimescaleDB size risks, missing retention/compression policies,
  index ratio hotspots, and temp-spill logging state.

## compute.scheduler-dbfree.env live-drift classification (2026-10-16)

At classification time the live node-22 `compute.scheduler-dbfree.env` lacked
the seven keys below that `compute.scheduler-dbfree.env.example` carries. None of
them is required: each row states what the loader applies when only that key is
absent (no legacy alias is honoured; see the note under the table). "effective
value when unset" comes from the loader source, not from the example:

- `NHMS_SCHEDULER_LEASE_GUARD_MODE`: `_file_lock_guard_mode()` in
  `services/orchestrator/scheduler_lease.py`.
- `NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS`,
  `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT`,
  `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`, `NHMS_SCHEDULER_TIMING_LEVEL`:
  the `ProductionSchedulerConfig` field defaults in
  `services/orchestrator/scheduler_config/config.py`.
- `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS`, `NHMS_SCHEDULER_EVIDENCE_MAX_MB`:
  `config_from_env` in `scripts/node22_scheduler_evidence_retention.py` (the
  evidence-retention unit, not the scheduler).

The classification itself changes no live value; a row whose key has since been
set live carries that bare assignment as its operator action. If an operator does
set a key, the line must be a bare `KEY=value`: systemd `EnvironmentFile=` treats
only whole-line comments as comments, so a trailing `# ...` becomes part of the
value.
`tests/test_node22_scheduler_env_drift_table.py` checks every row against the
loaders and the example.

| key | status | effective value when unset | operator action |
| --- | --- | --- | --- |
| `NHMS_SCHEDULER_LEASE_GUARD_MODE` | optional, example differs (example: atomic, default: flock) | `flock` | `NHMS_SCHEDULER_LEASE_GUARD_MODE=atomic` |
| `NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS` | optional (effective default: 300) | `300` | 保持未设置 |
| `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT` | optional (effective default: 3) | `3` | 保持未设置 |
| `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES` | optional (effective default: 3) | `3` | 保持未设置 |
| `NHMS_SCHEDULER_TIMING_LEVEL` | optional (effective default: stage) | `stage` | 保持未设置 |
| `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS` | optional (effective default: 90) | `90` | 保持未设置 |
| `NHMS_SCHEDULER_EVIDENCE_MAX_MB` | optional (effective default: 512) | `512` | 保持未设置 |

Note (#3146, set live 2026-11-16): the live lease guard used to come from the
legacy `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE=atomic` line, which
`scheduler_lease.py` honoured while the primary key was unset. At a pass
boundary that line was replaced in `compute.scheduler-dbfree.env` and
`compute.replay.env` by `NHMS_SCHEDULER_LEASE_GUARD_MODE=atomic`, so the lease
stays `atomic` and the journal stays `flock`. Both readers
(`scheduler_lease.py` and `file_orchestration_journal.py`) no longer honour the
legacy key: a config that sets it while the reader's primary key is unset (for
example a restored `.bak-*` copy) fails closed with an error naming the primary
key. When the primary key is set, the legacy line is ignored.

## Backup copies

Backup copies of env files (`*.bak*`, `*.backup-*`, `*.pre-*`) never live under
`infra/env/`. A copy left there sits next to the live files that units load
through `EnvironmentFile=` and may carry plaintext credentials.

- Home for backup copies: `~/NWM-presync-backup-<UTC date YYYY-MM-DD>/infra-env/`
  (the same dated directory used for pre-sync backups), mode `0700`.
- `scripts/ops/node22_env_backup_sweep.sh --env-dir <checkout>/infra/env` lists
  the backup copies directly under the env dir, one `plan:` line each, and moves
  nothing. Re-run it with `--apply` to move them with `mv -n` into the home
  above; nothing is ever deleted.
- The sweep exits 2 before any move when a copy is still named by an
  `EnvironmentFile=` line of an `nhms-*` unit or of one of its
  `nhms-*.service.d/*.conf` drop-ins (the unit is named on stderr), when a
  target already exists, when the unit directory holds no `nhms-*` unit, when
  `<backup-root>/infra-env` resolves under the env or unit directory or is a
  symlink, or when an `EnvironmentFile=` value cannot be parsed (a wildcard
  `*`/`?`/`[`, a specifier other than `%h`, or a line ending in `\`; stderr
  names the unit and the value).
- A failed `mv` midway or a receipt that cannot be written after a complete
  `--apply` also exits 2, after moves happened: the receipt's `moved` list is
  the record, and stderr prints one `moved: <name>` line per move as well (a
  zero-byte receipt is not a record; then stderr is). After `mv failed`,
  compare the target's size with the source's and delete a leftover partial
  target by hand before re-running (a cross-filesystem move that runs out of
  space leaves a truncated target that every later run reports as a conflict).
- The parser reads only the unit files on disk under `--systemd-user-dir`: not
  the state systemd loaded at its last `daemon-reload`, not non-`nhms-` units,
  not global `service.d/` drop-ins, not other unit search paths. It handles
  literal paths and `%h` only and refuses anything else. Before `--apply`,
  cross-check each unit with `systemctl --user show <unit> -p EnvironmentFiles`.
- Every run writes `sweep-receipt-<UTC %Y%m%dT%H%M%SZ>.json` into the same
  `infra-env/` directory (file names only, never file content), except the
  exits that happen before the receipt can exist: argument errors, the
  backup-directory refusals, a failed `mkdir -p`/`chmod 700`, and a receipt for
  the same second already present (stderr states the reason; nothing is
  written). Other defaults:
  `--backup-root ~/NWM-presync-backup-<UTC date>`, `--home $HOME` (only for
  `%h`), `--systemd-user-dir` `$XDG_CONFIG_HOME/systemd/user` when absolute,
  else `~/.config/systemd/user`. `--env-dir` has no default.
