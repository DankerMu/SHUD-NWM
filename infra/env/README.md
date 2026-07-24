# Two-node Docker environment examples

These files are examples for the M22 two-node Docker skeleton. They are safe
enough to render with `docker compose config`, but they are not final production
credentials.

Real local `compute.env`, `display.env`, and readonly validation
`display-readonly-secrets.env` files contain production secret-bearing values.
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

Compute role, node 22:

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
  `file`, and the registry/readiness/journal/state-index path variables from
  the checked-in `compute.example` matrix. `DATABASE_URL` may remain in
  `compute.env` only for `compute-api` or an explicit archived rollback drill;
  node-22 `:55433` is stopped/archived and is not scheduler runtime env.
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
