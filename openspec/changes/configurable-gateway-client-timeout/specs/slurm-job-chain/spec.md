## ADDED Requirements

### Requirement: The Slurm gateway client timeout SHALL be configurable and fail closed

The production orchestrator's `HttpSlurmGatewayClient` timeout SHALL come from `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS` when that variable is set and non-blank. When it is unset or blank, the timeout SHALL be 60 seconds.

A set value that is not a finite number greater than zero SHALL make configuration loading raise an error naming the variable. Such a value SHALL NOT fall back to the default and SHALL NOT be clamped.

Every rebuild of the orchestrator configuration on the scheduler path SHALL carry the configured timeout.

#### Scenario: The default timeout is unchanged
- **WHEN** `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS` is unset or blank
- **THEN** the orchestrator's gateway client timeout is 60 seconds

#### Scenario: A configured timeout reaches the scheduler's gateway client
- **WHEN** `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS=300`
- **AND** the scheduler builds its default orchestrator with Slurm execution enabled, for the configured source or for a different one
- **THEN** that orchestrator's gateway client timeout is 300 seconds

#### Scenario: An invalid timeout fails closed
- **WHEN** `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS` is `abc`, `0`, `-5`, `nan` or `inf`
- **THEN** loading the orchestrator configuration raises an error whose message names `SLURM_GATEWAY_CLIENT_TIMEOUT_SECONDS`
