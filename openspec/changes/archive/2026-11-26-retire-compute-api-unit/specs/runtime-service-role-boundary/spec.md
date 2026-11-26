## MODIFIED Requirements

### Requirement: The API service role is an explicit configuration

The system SHALL expose a single runtime service role contract that distinguishes local monolith, compute control, display readonly, and Slurm gateway execution modes.

#### Scenario: Local development default

- **WHEN** the API starts without `NHMS_SERVICE_ROLE` in a non-production local/test environment
- **THEN** it uses `dev_monolith`
- **AND** existing local tests can still exercise mock Slurm and mutating workflows.

#### Scenario: Production role required

- **WHEN** the API starts with `NHMS_REQUIRE_SERVICE_ROLE=true` or with production auth mode such as `NHMS_AUTH_MODE=production`, `live`, or `live_idp` and without `NHMS_SERVICE_ROLE`
- **THEN** startup fails with a clear configuration error
- **AND** the service does not silently fall back to a role that exposes control-plane capabilities.

#### Scenario: Units and env templates set explicit roles

- **WHEN** a tracked Slurm gateway unit that fixes its role or the display env template starts a service
- **THEN** it sets an explicit `NHMS_SERVICE_ROLE` matching the service being started
- **AND** the display env template also sets `NHMS_REQUIRE_SERVICE_ROLE=true`
- **AND** no tracked unit starts the API in the `compute_control` role: the compute API unit is retired (#3394) and the role remains a supported value of `NHMS_SERVICE_ROLE`.

#### Scenario: Unknown role rejected

- **WHEN** `NHMS_SERVICE_ROLE` is set to an unsupported value
- **THEN** startup fails with a clear configuration error
- **AND** no API routes are served.
