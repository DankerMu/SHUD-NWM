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
  `NHMS_REQUIRE_SERVICE_ROLE=true`, writer-capable `DATABASE_URL`,
  `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`, `NHMS_PUBLISHED_ARTIFACT_ROOT`, and
  `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT`.
- Scheduler no-flag business validation requires `NHMS_SCHEDULER_LOCK_ROOT`,
  `NHMS_SCHEDULER_EVIDENCE_ROOT`, `NHMS_SCHEDULER_RUNTIME_ROOT`,
  `NHMS_SCHEDULER_TEMP_ROOT`, and source/model filter envs such as
  `NHMS_SCHEDULER_SOURCES`, `NHMS_SCHEDULER_MODEL_IDS`,
  `NHMS_SCHEDULER_BASIN_IDS`, `NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE`,
  `NHMS_SCHEDULER_INTERVAL_SECONDS`, and `NHMS_SCHEDULER_MAX_PASSES`.
  Lock and evidence roots must be under `WORKSPACE_ROOT`; explicit
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
  readonly published artifact mount.
- Forbidden env keys must match `infra/docker/entrypoint.sh` and
  `scripts/validate_two_node_docker_runtime.py`: `SLURM_GATEWAY_URL`,
  `SLURM_GATEWAY_BACKEND`, `SLURM_GATEWAY_TEMPLATE_DIR`,
  `SLURM_GATEWAY_WORKSPACE_DIR`, `WORKSPACE_ROOT`, `RUN_WORKSPACE_ROOT`,
  `SHARED_LOG_ROOT`, `OBJECT_STORE_ROOT`, `NHMS_BASINS_ROOT`,
  `NHMS_MODEL_ASSET_ROOT`, `SHUD_EXECUTABLE`, `MUNGE_SOCKET`, `MUNGE_KEY`, and
  `DOCKER_HOST`.
- Forbidden container/host surfaces: `/etc/slurm`, `/run/munge`,
  `/var/run/munge`, `/etc/munge`, `munge.key`, `.nhms-runs`,
  `/run/docker.sock`, `/var/run/docker.sock`, and 22 private `/scratch` mounts.
- The display compose filesystem surface is a strict allowlist: exactly one
  `type: bind` mount from `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` to
  `NHMS_PUBLISHED_ARTIFACT_ROOT`, marked read-only. Extra binds, named
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
