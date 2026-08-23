# compute-scheduler-operationalization Specification Delta

## ADDED Requirements

### Requirement: The orchestrator's minimum Slurm poll interval is enforced on every path that reaches production

The forecast orchestrator SHALL NOT poll the Slurm gateway more often than once
per second on any path reachable in production. Every construction of the
orchestrator configuration that derives its poll interval from the deployment
environment SHALL apply that one-second minimum, and any construction that
copies a poll interval from such a configuration SHALL propagate the already
enforced value rather than re-deriving it from an unbounded source.

The minimum is a floor, never a rewrite: a configured interval at or above the
minimum SHALL be honored verbatim, and a value below it — including zero and
negative values — SHALL be raised to the minimum rather than rejected, so that a
mis-set deployment variable degrades to safe polling instead of failing the
orchestrator at start.

A configuration constructed with an explicitly supplied poll interval, which is
how tests drive the poll loop, SHALL receive that value unmodified. Test code
therefore MAY disable the polling delay, and the delay it disables MUST be a
real delay rather than one silently reinstated by the configuration object.

#### Scenario: The deployment environment sets no poll interval

- **WHEN** the orchestrator configuration is built from the environment and the
  poll-interval variable is unset
- **THEN** the configured default interval is used

#### Scenario: The deployment environment sets an interval below the minimum

- **WHEN** the orchestrator configuration is built from the environment and the
  poll-interval variable is zero, a fraction of a second, or negative
- **THEN** the resulting interval is the one-second minimum, and the orchestrator
  starts normally rather than raising

#### Scenario: The deployment environment sets an interval above the minimum

- **WHEN** the orchestrator configuration is built from the environment and the
  poll-interval variable is above one second
- **THEN** that value is used unchanged, because the minimum is a floor and not a
  normalization

#### Scenario: A production configuration is rebuilt from another configuration

- **WHEN** a production code path constructs a new orchestrator configuration by
  copying the poll interval from one that was built from the environment
- **THEN** the new configuration carries the same enforced interval, so no
  production path can hold a value that never passed the floor

#### Scenario: A test constructs a configuration with an explicit poll interval

- **WHEN** an orchestrator configuration is constructed with the poll interval
  passed explicitly rather than read from the environment
- **THEN** the supplied value is used exactly as given, including zero, so the
  poll loop performs no wall-clock wait

#### Scenario: The minimum is removed or bypassed

- **WHEN** the enforcement of the one-second minimum is deleted, weakened, or
  moved off the environment-derived construction path
- **THEN** a regression test fails, so the invariant cannot be lost silently
