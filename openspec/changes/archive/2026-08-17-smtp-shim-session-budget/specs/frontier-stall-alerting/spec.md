## MODIFIED Requirements
### Requirement: Alerting internal failures SHALL only increase alerting, never suppress it

The stall clock and the alert channel SHALL be fail-safe in the over-reporting direction: a corrupt or schema-mismatched state file — including byte-level (non-UTF-8) and parser-resource corruption, with no exception class escaping the run — SHALL immediately produce a monitoring-degraded alert and an honestly recorded baseline rebuild (never a silent clock reset), while a missing state file SHALL bootstrap the baseline silently with the bootstrap recorded (a fresh installation is not a degradation); a baseline SHALL only ever be established from a real observation — when a rebuild or bootstrap coincides with a failed observation the baseline stays pending and the next successful observation fills it without counting as progress; every blocking call in a tick (database connect and statement, sendmail) SHALL be time-bounded so a hang becomes a visible failure; an observation query failure SHALL never reset the stall clock and consecutive failures beyond the configured tick budget SHALL produce an observability alert; a failed email send SHALL NOT be recorded as delivered, so the next tick retries (sole exception: the recovery email, whose send failure is recorded but not retried). Error text emitted to any outlet (email body, log, receipt, state) SHALL have database connection strings redacted, and recipient/sender configuration containing header-breaking control characters SHALL be rejected before any send.


The email shim's SMTP session SHALL additionally be bounded by an explicit session budget (default 45 s, env-overridable at a single alignment point) strictly below the lane's sendmail wall: on budget expiry the shim SHALL exit on its own with the structured line `SMTP-FAILED stage=<stage> ... reason=session-budget` (exit 69) so that stage attribution survives every timeout path — including a peer that keeps resetting the per-operation socket timeout mid-operation — and the lane's SIGKILL wall is demoted to a fallback for a shim that itself hung.

#### Scenario: Corrupt state over-reports instead of silently resetting

- **WHEN** the persisted state file is unparsable (including non-UTF-8
  bytes or pathological nesting) or carries an unknown schema version
- **THEN** a monitoring-degraded alert email is sent, the baseline is
  rebuilt from the current observation with the reset recorded (or
  left pending when no observation is obtainable, so the next
  successful observation cannot masquerade as progress), and no
  exception escapes the run

#### Scenario: A missing state file bootstraps silently

- **WHEN** no state file exists at the configured path (first
  installation or a relocated state path)
- **THEN** the baseline is established from the current observation
  with the bootstrap recorded in the receipt and no alert email is
  sent

#### Scenario: Query failures keep the stall clock honest

- **WHEN** observation queries fail on consecutive ticks spanning the
  stall window
- **THEN** the stall clock continues from the last recorded change (a
  stall alert still fires on time) and an observability alert is sent
  after the configured consecutive-failure budget

#### Scenario: Send failure is retried, credentials never leak

- **WHEN** the sendmail invocation exits non-zero, or any failure path
  formats an error containing the database connection string
- **THEN** the alert is not recorded as delivered and is retried on
  the next tick, and every emitted text outlet carries the redacted
  form of the connection string

#### Scenario: Session budget expires before the lane wall with stage attribution

- **WHEN** the SMTP peer is slow enough that cumulative per-operation
  waits would exceed the lane's sendmail wall
- **THEN** the shim exits 69 before the wall with
  `SMTP-FAILED stage=<current stage> reason=session-budget` on stderr,
  the lane records the failure with the stage line (not a bare
  rc=124), and the failed send is not recorded as delivered

#### Scenario: A dribbling peer cannot escape the budget

- **WHEN** the peer trickles data so every per-operation timeout is
  reset and no single socket operation ever times out
- **THEN** the shim is interrupted by its own session budget within
  the budget window and exits 69 with stage attribution, without
  relying on the lane's SIGKILL

#### Scenario: rc=124 without a stage line means the shim itself hung

- **WHEN** the lane's sendmail wall kills the shim and the receipt
  carries rc=124 with no `SMTP-FAILED stage=` line
- **THEN** the runbook directs the operator to read this as a
  shim-self-hang, to treat the message as possibly already delivered
  (final-dot responsibility may have transferred), and to expect the
  next tick's retry to potentially duplicate the alert by design
