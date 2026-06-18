# runtime-service-role-boundary Specification

## Purpose
TBD - created by archiving change m22-two-node-docker-readonly-display. Update Purpose after archive.
## Requirements
### Requirement: Service role configuration

The system SHALL expose a single runtime service role contract that distinguishes local monolith, compute control, display readonly, and Slurm gateway execution modes.

#### Scenario: Local development default
- **WHEN** the API starts without `NHMS_SERVICE_ROLE` in a non-production local/test environment
- **THEN** it uses `dev_monolith`
- **AND** existing local tests can still exercise mock Slurm and mutating workflows.

#### Scenario: Production role required
- **WHEN** the API starts with `NHMS_REQUIRE_SERVICE_ROLE=true` or with production auth mode such as `NHMS_AUTH_MODE=production`, `live`, or `live_idp` and without `NHMS_SERVICE_ROLE`
- **THEN** startup fails with a clear configuration error
- **AND** the service does not silently fall back to a role that exposes control-plane capabilities.

#### Scenario: Docker and systemd set explicit roles
- **WHEN** Docker compose or systemd examples start an API service
- **THEN** they set `NHMS_REQUIRE_SERVICE_ROLE=true`
- **AND** they set an explicit `NHMS_SERVICE_ROLE` matching the service being started.

#### Scenario: Unknown role rejected
- **WHEN** `NHMS_SERVICE_ROLE` is set to an unsupported value
- **THEN** startup fails with a clear configuration error
- **AND** no API routes are served.

### Requirement: Slurm route exposure by role

The API SHALL mount Slurm control routes only for roles that are allowed to expose control-plane behavior.

#### Scenario: Display readonly has no Slurm routes
- **WHEN** the API starts with `NHMS_SERVICE_ROLE=display_readonly`
- **THEN** `/api/v1/slurm/*` routes are not registered
- **AND** the display-mode OpenAPI schema does not advertise Slurm operations.

#### Scenario: Compute control can expose Slurm routes
- **WHEN** the API starts with `NHMS_SERVICE_ROLE=compute_control`
- **THEN** Slurm routes can be registered according to existing gateway configuration
- **AND** existing control-plane tests can call the Slurm health route.

#### Scenario: Dev monolith remains compatible
- **WHEN** the API starts with `NHMS_SERVICE_ROLE=dev_monolith`
- **THEN** existing local Slurm mock and integration tests keep their current route surface unless a test explicitly overrides the role.

### Requirement: Display role unsafe configuration guard

The display readonly role SHALL reject or clearly block configuration that would give 27 compute-control capability.

#### Scenario: Display role has Slurm gateway configured
- **WHEN** `NHMS_SERVICE_ROLE=display_readonly` and `SLURM_GATEWAY_URL` or `SLURM_GATEWAY_BACKEND=slurm` is configured
- **THEN** startup fails or the configuration is reported as a blocker before serving requests
- **AND** retry/cancel and `/api/v1/slurm/*` remain unavailable.

#### Scenario: Display role has forbidden compute paths
- **WHEN** `NHMS_SERVICE_ROLE=display_readonly` and compute-only paths such as `WORKSPACE_ROOT`, `NHMS_BASINS_ROOT`, or `SHUD_EXECUTABLE` are configured as active dependencies
- **THEN** startup or preflight reports a high-severity display boundary blocker
- **AND** the service does not rely on those paths for display readiness.

### Requirement: Runtime config API

The API SHALL expose a read-only runtime config contract for frontend capability gating.

#### Scenario: Display runtime config
- **WHEN** the frontend requests runtime configuration from a display readonly API
- **THEN** the response identifies `service_role=display_readonly`
- **AND** it reports `control_mutations_enabled=false`, `slurm_routes_enabled=false`, and a display-safe queue-depth mode.

#### Scenario: Compute runtime config
- **WHEN** the frontend requests runtime configuration from a compute-control or dev API
- **THEN** the response identifies the current service role
- **AND** it reports whether control mutations and Slurm routes are enabled for that role.

### Requirement: Slurm gateway role is bounded

The `slurm_gateway` role SHALL not accidentally start the full business API surface.

#### Scenario: Reserved gateway role
- **WHEN** an implementation has not added a dedicated Slurm Gateway ASGI app
- **THEN** `NHMS_SERVICE_ROLE=slurm_gateway` is treated as a reserved role for host-service documentation
- **AND** startup fails clearly rather than serving forecast, pipeline, model, or frontend routes.

#### Scenario: Dedicated gateway app
- **WHEN** a dedicated Slurm Gateway app is implemented in this change
- **THEN** its route inventory contains only health and `/api/v1/slurm/*` gateway routes
- **AND** it does not expose business read or write APIs.

