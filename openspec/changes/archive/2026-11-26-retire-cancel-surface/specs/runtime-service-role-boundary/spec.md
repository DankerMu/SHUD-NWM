## MODIFIED Requirements

### Requirement: Display role unsafe configuration guard

The display readonly role SHALL reject or clearly block configuration that would give 27 compute-control capability.

#### Scenario: Display role has Slurm gateway configured
- **WHEN** `NHMS_SERVICE_ROLE=display_readonly` and `SLURM_GATEWAY_URL` or `SLURM_GATEWAY_BACKEND=slurm` is configured
- **THEN** startup fails or the configuration is reported as a blocker before serving requests
- **AND** retry and `/api/v1/slurm/*` remain unavailable.

#### Scenario: Display role has forbidden compute paths
- **WHEN** `NHMS_SERVICE_ROLE=display_readonly` and compute-only paths such as `WORKSPACE_ROOT`, `NHMS_BASINS_ROOT`, or `SHUD_EXECUTABLE` are configured as active dependencies
- **THEN** startup or preflight reports a high-severity display boundary blocker
- **AND** the service does not rely on those paths for display readiness.
