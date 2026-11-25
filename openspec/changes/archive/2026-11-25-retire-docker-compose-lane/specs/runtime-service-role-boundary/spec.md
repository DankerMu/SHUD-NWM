## REMOVED Requirements

### Requirement: Service role configuration

**Reason**: One scenario is named after Docker compose, which is retired (#3231).

**Migration**: Replaced by "The API service role is an explicit configuration" with the same scenarios; the one about deployment files now speaks of systemd units and env templates.

## ADDED Requirements

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

- **WHEN** the tracked compute API unit, a tracked Slurm gateway unit that fixes its role, or the display env template starts a service
- **THEN** it sets an explicit `NHMS_SERVICE_ROLE` matching the service being started
- **AND** the display env template also sets `NHMS_REQUIRE_SERVICE_ROLE=true`.

#### Scenario: Unknown role rejected

- **WHEN** `NHMS_SERVICE_ROLE` is set to an unsupported value
- **THEN** startup fails with a clear configuration error
- **AND** no API routes are served.

### Requirement: The display env template is a configuration the runtime guard accepts

`infra/env/display.example`, the template of the live `infra/env/display.env`,
SHALL set `NHMS_SERVICE_ROLE=display_readonly`, `NHMS_REQUIRE_SERVICE_ROLE=true`,
`NHMS_AUTH_MODE=production`, `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true`,
`NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false`, `NHMS_DISPLAY_API_PORT=8080` and a
non-empty `OBJECT_STORE_ROOT`, and SHALL set no key that the display role
refuses at startup. Its storage roots SHALL be node-27 host paths
(`OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`,
`NHMS_PUBLISHED_ARTIFACT_ROOT=/home/ghdc/nwm/published`), not paths inside a
container. The template SHALL NOT carry a key that only the retired
docker/compose lane read.

`infra/env/README.md` SHALL list the keys the display role refuses at startup:
exactly the keys the startup guard refuses, no more and no fewer.

#### Scenario: The runtime guard accepts the template

- **WHEN** the template's assignments are given to the display role's startup guard
- **THEN** the guard reports no blocker
- **AND** the seven keys and the two storage roots above have the values above

#### Scenario: A forbidden key in the template is caught

- **WHEN** a key that the display role refuses at startup is added to the template's assignments
- **THEN** the guard reports a blocker for that key

#### Scenario: The README lists exactly the refused keys

- **WHEN** the forbidden-key list of `infra/env/README.md` is compared with the keys the display role refuses at startup
- **THEN** the two sets are equal
