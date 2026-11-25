## ADDED Requirements

### Requirement: A scheduler configured for the slurm backend submits only through a gateway on the slurm backend
The backend name SHALL have two values, `mock` and `slurm`. When the scheduler is configured for the `slurm` backend, its gateway probe SHALL treat a health body that names any other backend as unhealthy and not submit-capable, SHALL record the reported backend in the gateway check, and the pass SHALL carry the `SLURM_GATEWAY_UNAVAILABLE` blocker with a reason that names the reported backend. A health body without a backend name SHALL be judged on its health and binaries alone. A scheduler configured with a backend name other than `mock` or `slurm` SHALL NOT probe over HTTP and SHALL get an unhealthy gateway check. The in-process probe used with the `mock` backend in development and tests SHALL be unchanged.

#### Scenario: Remote gateway on the mock backend
- **GIVEN** a scheduler configured for the `slurm` backend with a gateway URL and a service token
- **WHEN** the gateway's health body reports `healthy` true, all four binaries executable and `backend` `mock`
- **THEN** the gateway check is unhealthy and not submit-capable, records backend `mock`, and the pass carries `SLURM_GATEWAY_UNAVAILABLE` with a reason naming `mock`

#### Scenario: Remote gateway on the slurm backend
- **GIVEN** the same scheduler
- **WHEN** the health body reports `healthy` true, all four binaries executable and `backend` `slurm`
- **THEN** the gateway check is healthy and submit-capable, as before

#### Scenario: Health body without a backend name
- **GIVEN** the same scheduler
- **WHEN** the health body is healthy with all four binaries executable and carries no `backend` key
- **THEN** the gateway check is healthy and submit-capable, as before

#### Scenario: Unknown backend name on the scheduler
- **GIVEN** a scheduler whose environment sets the backend to `real`
- **WHEN** the gateway check runs
- **THEN** no HTTP request is made and the gateway check is unhealthy with the `SLURM_GATEWAY_UNAVAILABLE` blocker

#### Scenario: Development default
- **GIVEN** a scheduler with no backend configured
- **WHEN** the gateway check runs
- **THEN** it uses the in-process mock gateway and is healthy, as before
