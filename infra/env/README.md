# Two-node Docker environment examples

These files are examples for the M22 two-node Docker skeleton. They are safe
enough to render with `docker compose config`, but they are not final production
credentials.

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
  `WORKSPACE_ROOT`, `NHMS_PUBLISHED_ARTIFACT_ROOT`, and
  `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT`.
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
- Forbidden: `SLURM_GATEWAY_URL`, `SLURM_GATEWAY_BACKEND`, `WORKSPACE_ROOT`,
  `OBJECT_STORE_ROOT`, `NHMS_BASINS_ROOT`, `NHMS_MODEL_ASSET_ROOT`,
  `SHUD_EXECUTABLE`, `/etc/slurm`, `/run/munge`, `/var/run/munge`,
  `/etc/munge`, `munge.key`, `.nhms-runs`, `/var/run/docker.sock`, and 22
  private `/scratch` mounts.
- The display compose filesystem surface is a strict allowlist: exactly one
  `type: bind` mount from `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` to
  `NHMS_PUBLISHED_ARTIFACT_ROOT`, marked read-only. Extra binds, named
  volumes, relative bind sources, local named-volume bind devices, and tmpfs
  entries below the published artifact root are validation failures. Display
  `configs`, `secrets`, `devices`, `device_cgroup_rules`, and
  `device_requests` are not allowed.
- The display service must keep `read_only: true`, `cap_drop: [ALL]`, and
  exactly `security_opt: [no-new-privileges:true]` as literal compose values.
- Run static validation from the same shell used for compose rendering. Ambient
  process environment overrides for any compute or display compose
  interpolation variable are treated as static failures when that variable is
  present in the corresponding env file and the process value differs. This
  covers mount roots, image/tag/user/port variables, `DATABASE_URL`,
  `NHMS_AUTH_MODE`, published artifact metadata, and role/safety flags.
  Display audited runtime env keys must be literal values or interpolate through
  their same canonical env key; null imports and alias variables such as
  `DISPLAY_DATABASE_URL` are rejected.

Validation commands:

```bash
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
`compute.env`, `display.env`, and `local.env` are intentionally ignored while
`*.example` and this README remain trackable. `infra/docker-compose.dev.yml`
remains a local development dependency stack and must not be used as either
production two-node compose file.
