## ADDED Requirements

### Requirement: One app image with role-specific startup

The Docker runtime SHALL build one default application image that can start role-specific commands through environment configuration.

#### Scenario: App image build
- **WHEN** `infra/docker/Dockerfile.app` is built
- **THEN** the image includes backend runtime dependencies and frontend static assets as required for MVP
- **AND** it does not install Slurm client or Munge by default.

#### Scenario: Role entrypoint
- **WHEN** the container starts with `NHMS_SERVICE_ROLE=display_readonly`
- **THEN** the entrypoint starts the display API/frontend service path
- **AND** it rejects compute-control-only commands or missing display requirements.

#### Scenario: Scheduler once command
- **WHEN** the compute compose runs a scheduler task
- **THEN** it uses an existing tested entrypoint such as `nhms-pipeline plan-production --plan`
- **AND** it does not reference a long-running scheduler loop unless that entrypoint exists and has tests.

### Requirement: Compute compose has write-capable production mounts

The compute compose file SHALL express the 22 node's compute-control capability without exposing it to 27.

#### Scenario: Compute compose mounts
- **WHEN** `infra/compose.compute.yml` is rendered or validated
- **THEN** compute services can mount Basins/model assets read-only, workspace read-write, and published artifact root read-write
- **AND** they use `NHMS_SERVICE_ROLE=compute_control`.

#### Scenario: Compute compose uses canonical publish-root names
- **WHEN** `infra/compose.compute.yml` is rendered or validated
- **THEN** host publish-root source configuration uses `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` when it differs from the in-container root
- **AND** the container runtime target is `NHMS_PUBLISHED_ARTIFACT_ROOT`.

#### Scenario: Compute compose network exposure
- **WHEN** compute API or gateway ports are configured
- **THEN** they bind to localhost or an explicit internal control network by default
- **AND** the compose docs warn against exposing control endpoints publicly.

### Requirement: Role-specific env examples and Docker preflight

The Docker runtime SHALL provide role-specific env examples and preflight checks before large Docker work.

#### Scenario: Role-specific env examples
- **WHEN** `infra/env/compute.example`, `infra/env/display.example`, and shared env documentation are checked
- **THEN** they use canonical `NHMS_PUBLISHED_ARTIFACT_ROOT`, `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX`, `NHMS_PUBLISHED_ARTIFACT_S3_BUCKET`, `NHMS_PUBLISHED_ARTIFACT_S3_PREFIX`, and optional `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT`
- **AND** they document required and forbidden variables for compute and display roles.

#### Scenario: Docker disk preflight
- **WHEN** Docker preflight runs before build or smoke work
- **THEN** it records `docker version`, `docker compose version`, DockerRootDir, `docker system df`, `df -h`, `TMPDIR`, and the evidence root
- **AND** low space is reported as `BLOCKED` before build or smoke work continues.

#### Scenario: Dev compose is not production two-node compose
- **WHEN** production two-node static checks are run
- **THEN** `infra/docker-compose.dev.yml` is rejected as a production compute/display compose input
- **AND** the dev compose file remains available only for local development dependencies.

### Requirement: Display compose has no physical control capability

The display compose file SHALL encode 27 as a physically read-only display service.

#### Scenario: Display compose forbidden mounts
- **WHEN** `infra/compose.display.yml` is rendered or validated
- **THEN** the display service does not mount `/etc/slurm`, `/run/munge`, `WORKSPACE_ROOT`, `NHMS_BASINS_ROOT`, `/var/run/docker.sock`, `.nhms-runs`, or 22 private `/scratch`
- **AND** the published artifact mount is read-only.

#### Scenario: Display compose forbidden env
- **WHEN** display env examples are checked
- **THEN** they do not configure `SLURM_GATEWAY_URL`, `SLURM_GATEWAY_BACKEND=slurm`, `WORKSPACE_ROOT`, `NHMS_BASINS_ROOT`, or `SHUD_EXECUTABLE`
- **AND** they set `NHMS_SERVICE_ROLE=display_readonly`, `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true`, and `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false`.

#### Scenario: Display container security probe
- **WHEN** the display container is started in a Docker smoke test
- **THEN** checks show no `sbatch` or `scancel` executable, no `/etc/slurm/slurm.conf`, no Munge socket, and no Docker socket
- **AND** `/api/v1/slurm/*` is unavailable.

#### Scenario: Display HostConfig isolation
- **WHEN** `infra/compose.display.yml` is rendered and the display container is inspected
- **THEN** display services are not privileged
- **AND** they do not use host PID, host IPC, host network, broad host-root bind mounts, Docker socket mounts, or `cap_add`
- **AND** the display API uses a readonly root filesystem where feasible.

#### Scenario: Display published root readonly
- **WHEN** the display container is inspected
- **THEN** the published artifact mount is readonly
- **AND** its in-container target matches `NHMS_PUBLISHED_ARTIFACT_ROOT`.

### Requirement: Systemd and deployment docs

The Docker runtime SHALL include operator-facing systemd units and two-node Docker documentation.

#### Scenario: systemd units
- **WHEN** systemd unit examples are added
- **THEN** they start compute and display compose files from the repository `infra` directory
- **AND** the Slurm Gateway host-service unit is documented as the MVP-recommended first phase if independent gateway containerization is not yet proven.

#### Scenario: Two-node Docker README
- **WHEN** `infra/README.two-node-docker.md` is added
- **THEN** it documents 22/27 responsibilities, environment files, compose commands, scratch/evidence directories, security checks, and rollback
- **AND** it states that the dev compose file is not a production two-node deployment.
