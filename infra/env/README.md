# Two-node Docker environment examples

These files are examples for the M22 two-node Docker skeleton. They are safe
enough to render with `docker compose config`, but they are not final production
credentials.

Real local `compute.env`, `display.env`, and readonly validation
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
`install -m 0600 infra/env/compute.example infra/env/compute.env` or
`install -m 0600 /dev/null infra/env/display-readonly-secrets.env`, or under
`umask 077`, and keep them untracked. Before sourcing any local secret-source
file, use a fail-closed guard that checks the file exists, verifies `stat -c
'%a' <file>` is `600`, prints `BLOCKED:`, and exits before `source` if the
check fails. A failed mode check blocks validation rather than producing
evidence from a readable secret file. Before any direct Docker Compose command
with `compute.env`/`display.env`, or before systemd install/start/restart, run
the checked-in source-trust preflight
`scripts/validate_two_node_docker_source_trust.py`; it checks checkout path
components, compose/unit/env sources, trusted owners, symlinks, group/world
writes, and role env mode `0600` before Docker can consume those files.

Required canonical published artifact variables:

- `NHMS_PUBLISHED_ARTIFACT_ROOT`: in-container published artifact root used by
  the app.
- `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX`: canonical URI prefix, normally
  `published://`.
- `NHMS_PUBLISHED_ARTIFACT_S3_BUCKET`: optional allowlisted bucket for published
  artifact reads.
- `NHMS_PUBLISHED_ARTIFACT_S3_PREFIX`: optional allowlisted prefix for published
  artifact reads.
- `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT`: compose-only host bind source when it
  differs from the in-container root.

Do not use unprefixed `PUBLISHED_ARTIFACT_ROOT` as an app runtime variable.

## node-22 unit → EnvironmentFile authority (#1694)

Which file a node-22 user systemd unit actually loads. Paths are on node-22
under `/scratch/frd_muziyao/`. Verified read-only on the live host 2026-09-02.

| Unit (`systemctl --user`) | EnvironmentFile(s) loaded | Tracked template |
|---|---|---|
| `nhms-compute-scheduler.service` | `NWM/infra/env/compute.scheduler-dbfree.env` + `nhms-prod/secrets/slurm-gateway.env` | `compute.scheduler-dbfree.env.example`; the secret file has none by design (credential-bearing, mode 0600, see #1684 above) |
| `nhms-scheduler-evidence-retention.service` | `NWM/infra/env/compute.scheduler-dbfree.env` | `compute.scheduler-dbfree.env.example` |
| `nhms-scheduler-file-provider-refresh.service` | `NWM/infra/env/compute.scheduler-provider-refresh.env` | `compute.scheduler-provider-refresh.env.example` |
| `nhms-compute-api.service` | `NWM/infra/env/compute.host.env` | **untracked, no template** |
| `nhms-slurm-gateway.service` | `NWM/infra/env/compute.host.env` + `nhms-prod/secrets/slurm-gateway.env` | **untracked, no template** for `compute.host.env` |

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
- **`infra/env/compute.env` is the compose-lane instance of `compute.example`.**
  Verified on node-22 read-only 2026-09-02, receipt
  `.workplans/pr-1956/node22-compute-env-receipt.log`: no user systemd unit
  lists it in `EnvironmentFiles` (receipt `:1-13`, the five units in the table
  above), and `docker ps` on the node-22 login host (`xnode`) returned no
  container rows at all, so no running container reads it (receipt `:98-100`).
  Values in it — including basin roots and scheduler model/basin filters — are
  **not** the production scheduler's configuration and must not be quoted as
  such.

  It is not unreferenced, though. Who else names it:

  - `infra/systemd/nhms-compute-compose.service` (tracked, **not installed** on
    node-22: `LoadState=not-found` in both the user and the system manager,
    receipt `:101-106`) and `infra/README.two-node-docker.md` describe the
    compose lane.
  - `scripts/validate_two_node_docker_source_trust.py:190` treats it as the
    compute role env for source-trust checks, and
    `tests/test_two_node_docker_source_trust.py:350`/`:392`/`:406`,
    `tests/test_two_node_docker_runtime.py:3725`, and
    `tests/test_two_node_docker_runbook_environment_invariant.py:548` exercise
    the instance / the `compute.example` → `compute.env` install step.
  - The `compute.example` template itself is parsed by
    `tests/test_two_node_docker_runtime.py` (55 call sites, including the
    display forbidden-mount fixture). **When you edit a value here, the local
    oracle is the grep-derived consumer set** — everything that names the
    template, from `grep -rln "compute\.example" tests/ scripts/` on 2026-09-02:
    `tests/test_two_node_docker_runtime.py`,
    `tests/test_two_node_docker_runbook_environment_invariant.py`,
    `tests/test_role_boundary_static.py`,
    `tests/test_slurm_gateway_deployment_contract.py`,
    `tests/test_two_node_e2e_evidence.py`, `tests/test_production_scheduler.py`,
    and `tests/test_select_ci_tests.py`, plus
    `scripts/validate_two_node_docker_runtime.py:5374`, which uses the template
    as the default `--compute-env` for the static check.
    `tests/test_two_node_docker_source_trust.py` is **not** in that set (it reads
    `compute.env`, not the template).
  - **On CI the authority is `scripts/select_ci_tests.py`**, where two rules match
    `infra/env/compute.example`: the `infra/env/**` glob (`:2301-2304` →
    `tests/test_two_node_docker_runtime.py`) and the exact-path rule
    (`:2313-2316` → `tests/test_slurm_gateway_deployment_contract.py`, the
    `SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST` constant at `:496`). The other
    consumer files are not selected for this path,
    so a green PR here does not exercise them — run the grep set locally.
- `compute.host.env` is untracked with no committed template. That gap is
  recorded, not fixed, by #1694.

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

The bullets below are the **compose-lane role contract** — what
`compute.example` / `compute.env` and `infra/compose.compute.yml` must express.
They are not a claim about which file a live node-22 unit loads; for that, the
unit → EnvironmentFile table above is the authority.

- Required: `NHMS_SERVICE_ROLE=compute_control`,
  `NHMS_REQUIRE_SERVICE_ROLE=true`, writer-capable `DATABASE_URL` for
  `compute-api` and rollback only,
  `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`,
  `NHMS_OBJECT_STORE_COPYBACK_ROOT`, `NHMS_PUBLISHED_ARTIFACT_ROOT`, and
  `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT`. `OBJECT_STORE_ROOT` is the
  compute-visible staging root; `NHMS_OBJECT_STORE_COPYBACK_ROOT` is the
  shared object-store mirror used after publish.
- The production `scheduler-once` service is DB-free: `infra/compose.compute.yml`
  does not pass `DATABASE_URL` to it. It must set
  `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, every scheduler backend selector to
  `file`, and the registry/readiness/journal/state-index path variables. On the
  live node-22 units those variables come from `compute.scheduler-dbfree.env`
  (tracked template `compute.scheduler-dbfree.env.example`), per the table
  above; `compute.example` carries the same key names for the compose lane only.
  The live file is untracked, so the template is *not* evidence for its
  contents. Read on node-22 2026-09-10 (`-rw-------` `frd_muziyao:huser`):
  `:13 OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store`,
  `:15 NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store` — both
  present, which is what the canonical-precip backfill recovery command in
  `docs/runbooks/current-production-ops.md` §5.3 depends on. Same scan: only
  `compute.host.env`, `compute.replay.env` and `compute.scheduler-dbfree.env`
  carry `NHMS_OBJECT_STORE_COPYBACK_ROOT`.
  `DATABASE_URL` belongs to the compose lane's `compute-api` or to an explicit
  archived rollback drill — and the live `nhms-compute-api.service` does not load
  `compute.env` at all; its only `EnvironmentFile` is the untracked
  `compute.host.env` (receipt `:9-10`). Node-22 `:55433` is stopped/archived and
  is not scheduler runtime env.
- The node-22 DB-free scheduler live env
  (`compute.scheduler-dbfree.env`, referenced by systemd `EnvironmentFile=`)
  has exactly one tracked source: `compute.scheduler-dbfree.env.example`.
  Its required key set is the union of two layers, both carried by that
  single template — do not source `compute.example` into the scheduler env
  (it carries `DATABASE_URL`, which violates the DB-free gate):
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
    900 s is sized against the measured hold: ~2.2 GB per acquisition at
    ~62 MB/s NFS throughput is ~36 s. The 900 s arithmetic assumed **two**
    acquisitions per cycle (`parse` and `state_save_qc`, ~72 s), which admits
    ~24 queued acquisitions ~= 12 concurrent execution units against the 2 of
    live steady state. Re-derived 2026-09-10, the run-tree lane takes **one**
    per cycle in both configurations — unset `NHMS_ORCHESTRATOR_TERMINAL_STAGE`
    hits only `parse`, and `forecast_state_save_qc` (what node-22 runs) makes
    `chain_stages.stages_through` drop `parse` from the stage list entirely, so
    only `state_save_qc` hits. The real headroom is therefore ~2x wider than
    those numbers; left unretuned because the error is in the safe direction.
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
  compatibility, not the compute scheduler-once proof path.
- Allowed only on compute: Slurm gateway settings, writable workspace,
  Basins/model asset paths, and `SHUD_EXECUTABLE`.
- The compute compose has no published host port by default. Keep any future
  control API or Slurm gateway listener on localhost or an internal control
  network.

Display role, node 27:

- Required: `NHMS_SERVICE_ROLE=display_readonly`,
  `NHMS_REQUIRE_SERVICE_ROLE=true`, `NHMS_AUTH_MODE=production`,
  `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true`,
  `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false`, readonly `DATABASE_URL`, and a
  readonly `OBJECT_STORE_ROOT`, plus readonly published artifact and
  object-store mounts.
- Forbidden env keys must match `infra/docker/entrypoint.sh` and
  `scripts/validate_two_node_docker_runtime.py`: `SLURM_GATEWAY_URL`,
  `SLURM_GATEWAY_BACKEND`, `SLURM_GATEWAY_TEMPLATE_DIR`,
  `SLURM_GATEWAY_WORKSPACE_DIR`, `WORKSPACE_ROOT`, `RUN_WORKSPACE_ROOT`,
  `SHARED_LOG_ROOT`, `NHMS_OBJECT_STORE_COPYBACK_ROOT`,
  `NHMS_SCHEDULER_LOCK_ROOT`, `NHMS_SCHEDULER_EVIDENCE_ROOT`,
  `NHMS_SCHEDULER_RUNTIME_ROOT`, `NHMS_SCHEDULER_TEMP_ROOT`,
  `NHMS_BASINS_ROOT`, `NHMS_MODEL_ASSET_ROOT`, `SHUD_EXECUTABLE`,
  `MUNGE_SOCKET`, `MUNGE_KEY`, and `DOCKER_HOST`.
- Forbidden container/host surfaces: `/etc/slurm`, `/run/munge`,
  `/var/run/munge`, `/etc/munge`, `munge.key`, `.nhms-runs`,
  `/run/docker.sock`, `/var/run/docker.sock`, and 22 private `/scratch` mounts.
- The display compose filesystem surface is a strict allowlist: exactly one
  `type: bind` mount from `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` to
  `NHMS_PUBLISHED_ARTIFACT_ROOT`, and one `type: bind` mount from
  `OBJECT_STORE_ROOT` to `OBJECT_STORE_ROOT`, both marked read-only. Extra binds, named
  volumes, relative bind sources, local named-volume bind devices, and tmpfs
  entries below the published artifact root are validation failures. Display
  `configs`, `secrets`, `deploy`, `devices`, `device_cgroup_rules`, and
  `device_requests` are not allowed.
- The display service must keep `read_only: true`, `cap_drop: [ALL]`, and
  exactly `security_opt: [no-new-privileges:true]` as literal compose values.
- Run static validation from the same shell used for compose rendering. For
  each role, every compose interpolation variable must be declared in that
  role's env file and must be part of the approved role contract. Ambient
  process environment overrides for any approved compose interpolation variable
  are static failures when the process value differs from the env-file value.
  This covers mount roots, image/tag/user/port variables, `DATABASE_URL`,
  `NHMS_AUTH_MODE`, published artifact metadata, object-store/CORS settings,
  and role/safety flags. Display audited runtime env keys must be literal
  values or interpolate through their same canonical env key; null imports and
  alias variables such as `DISPLAY_DATABASE_URL` are rejected.

Validation commands:

```bash
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$PWD" \
  --trust-root "$(dirname "$PWD")" \
  --evidence-root artifacts/two-node-e2e/source-trust \
  --trusted-owner "$(id -un)" \
  --role compute --role display
uv run python scripts/validate_two_node_docker_runtime.py static
uv run python scripts/validate_two_node_docker_runtime.py preflight
docker compose --env-file infra/env/compute.example -f infra/compose.compute.yml config
docker compose --env-file infra/env/display.example -f infra/compose.display.yml config
```

When `TMPDIR` is unset, preflight uses `artifacts/tmp` in this repository.
If `TMPDIR` is set explicitly, it must point under this repository's
`artifacts/` tree or under `/scratch/frd_muziyao` outside this checkout.

All generated validation evidence must stay under `artifacts/` in this
repository or under `/scratch/frd_muziyao` outside this checkout. Do not write
reports or evidence into `infra/env/`; real local env files such as
`compute.env`, `display.env`, `display-readonly-secrets.env`, and `local.env`
are intentionally ignored while `*.example` and this README remain trackable.
`infra/docker-compose.dev.yml` remains a local development dependency stack and
must not be used as either production two-node compose file.

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

Node-27 download role:

- `infra/env/node27-download.example` is the committed template for
  `scripts/node27_download_once.sh` and
  `infra/systemd/nhms-node27-download.{service,timer}`. Copy it to untracked
  `infra/env/node27-download.env` with mode `0600` and writer-capable
  `DATABASE_URL` on node-27.
- Leave `NODE27_DOWNLOAD_CYCLE_TIME` empty for production automation. The runner
  selects the first missing raw cycle after the existing contiguous raw chain,
  bounded by the latest allowed UTC cycle from
  `NHMS_NODE27_DOWNLOAD_ALLOWED_CYCLE_HOURS_UTC` after
  `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS` (default template: 8 hours). When no raw
  seed exists in the continuity lookback window, it selects the latest allowed
  cycle as the first seed. Set `NODE27_DOWNLOAD_CYCLE_TIME` only for explicit
  operator-directed runs.
- The download env is not display runtime config. It must use
  `NHMS_NODE27_DOWNLOAD_ROLE=node27_data_plane_download`, local node-27
  PostgreSQL `:55432`, `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`, and a
  node-27 local `WORKSPACE_ROOT`.

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
