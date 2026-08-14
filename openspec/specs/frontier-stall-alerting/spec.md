# frontier-stall-alerting Specification

## Purpose
TBD - created by archiving change node27-frontier-stall-alert. Update Purpose after archive.
## Requirements
### Requirement: The published frontier SHALL be watched progress-based and stalls SHALL alert by email

A periodic node-27 check (default cadence every 30 minutes) SHALL observe the published frontier from `hydro.hydro_run` restricted to post-ingest lifecycle rows (succeeded, parsed, published — lifecycle transitions inside that set do not alter the observation), taking per `source_id` the frontier (max `cycle_time`), the distinct `cycle_time` count, and the latest-arrival high-water (max `created_at`), null cycles excluded, and SHALL compare each observation against the persisted previous snapshot — never against wall-clock lag, because a backfilling frontier is legitimately days behind. Progress SHALL be directional: only a new source or a strict increase in one of a source's markers counts, while decreases (a row leaving the lifecycle set, manual deletion) SHALL NOT reset the stall clock and SHALL NOT lower the persisted per-source baseline (markers are high-water). When no source shows progress for at least the configured stall window (default 4 hours), the check SHALL send a stall alert email to the configured recipient; while the stall persists it SHALL resend at most once per resend window (default 6 hours), and when progress resumes it SHALL attempt exactly one recovery email (a failed recovery send is recorded but not retried and does not re-arm the alert — a stall that no longer exists must not be re-announced; the failure remains visible via the non-zero run status). The recipient address SHALL be explicit configuration with no default: a missing recipient or database URL SHALL fail the run as a structured configuration error before any observation.

#### Scenario: Stalled frontier alerts once and dedups

- **WHEN** the observation shows no directional progress against the
  persisted snapshot continuously for at least the stall window
- **THEN** exactly one stall alert email is sent at the threshold
  crossing, no resend occurs before the resend window elapses, and one
  resend occurs after it while the stall persists

#### Scenario: Any progress resets the clock and closes the loop

- **WHEN** the observation shows directional progress (a source's
  frontier advanced, its distinct-cycle count grew, its latest-arrival
  high-water rose, or a new source appeared)
- **THEN** no stall alert is sent, the persisted last-change timestamp
  resets, and if an alert was active exactly one recovery email is
  sent and the alert state clears

#### Scenario: Missing configuration fails closed

- **WHEN** the database URL or the recipient address is absent from
  the environment
- **THEN** the run exits non-zero with a structured configuration
  error and no email send is attempted

### Requirement: Alerting internal failures SHALL only increase alerting, never suppress it

The stall clock and the alert channel SHALL be fail-safe in the over-reporting direction: a corrupt or schema-mismatched state file — including byte-level (non-UTF-8) and parser-resource corruption, with no exception class escaping the run — SHALL immediately produce a monitoring-degraded alert and an honestly recorded baseline rebuild (never a silent clock reset), while a missing state file SHALL bootstrap the baseline silently with the bootstrap recorded (a fresh installation is not a degradation); a baseline SHALL only ever be established from a real observation — when a rebuild or bootstrap coincides with a failed observation the baseline stays pending and the next successful observation fills it without counting as progress; every blocking call in a tick (database connect and statement, sendmail) SHALL be time-bounded so a hang becomes a visible failure; an observation query failure SHALL never reset the stall clock and consecutive failures beyond the configured tick budget SHALL produce an observability alert; a failed email send SHALL NOT be recorded as delivered, so the next tick retries (sole exception: the recovery email, whose send failure is recorded but not retried). Error text emitted to any outlet (email body, log, receipt, state) SHALL have database connection strings redacted, and recipient/sender configuration containing header-breaking control characters SHALL be rejected before any send.

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

