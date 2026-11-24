## MODIFIED Requirements

### Requirement: Active node-22 entrypoints preserve the deferred environment

Every tracked automatic or required operator operation SHALL preserve the
active environment of the compute role's checkout, which Slurm jobs and
scheduler timers on `w3` and `w2` reach over NFS. It uses a checked-in wrapper
or the exact active `.venv` interpreter and SHALL NOT create, update,
synchronize, or replace that environment. The single exception is a deploy
that changes `uv.lock`: the operator MAY synchronize the environment while the
scheduler timer is stopped, no scheduler pass is in flight, and no NWM job is
in the Slurm queue.

#### Scenario: Exact interpreter is required

- **GIVEN** the active checkout's environment is shared by running timers and
  Slurm jobs
- **WHEN** an automatic unit or required operator command runs
- **THEN** it invokes exact active Python or a checked-in wrapper
- **AND** a missing interpreter fails closed without invoking environment-updating uv

#### Scenario: Lockfile deploy is the only synchronization window

- **WHEN** agent instructions or a current runbook describe synchronizing the
  compute role's environment
- **THEN** they limit it to a deploy that changes `uv.lock`
- **AND** they require all three conditions: scheduler timer stopped, no pass
  in flight, no NWM job in the Slurm queue
- **AND** they forbid running any uv command on the retired host
  `210.77.77.22`

#### Scenario: Isolated rollback remains isolated

- **WHEN** a rollback checkout explicitly synchronizes its own environment
- **THEN** the command remains scoped to that rollback checkout
- **AND** it does not authorize synchronization of the active checkout
