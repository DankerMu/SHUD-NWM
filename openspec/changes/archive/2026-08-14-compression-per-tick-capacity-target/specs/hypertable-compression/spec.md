# hypertable-compression delta（compression-per-tick-capacity-target，#1237）

## ADDED Requirements

### Requirement: The compression per-tick bound MUST be a capacity-derived target consistent across template, live env, and receipts

The per-tick bound SHALL be a decided capacity target derived from
measured inputs (steady-state terminal-chunk arrival rate, the
retention-window backlog ceiling, and the relation between observed
per-chunk compression duration and the wrapper wall — the wall bounds
the WHOLE tick, so the bound is a throughput ceiling, not a redeemable
single-tick capacity; catch-up under backlog follows the runbook's
catch-up recipe rather than relying on the bound), not an arbitrary
default or an unrecorded live retune: the
committed env template SHALL carry the target value with a comment
identifying it as a capacity conclusion and pointing at the recorded
derivation in the operator runbook, the deployed node-27 env SHALL carry
the same value, and every receipt echoes the effective bound via its
existing `per_tick_bound` field. The variable remains mandatory with no
in-code default.

#### Scenario: Template carries the pinned capacity target

- **WHEN** `infra/env/node27-timeseries-compression.example` is read
- **THEN** it SHALL contain the uncommented line
  `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=4` with a
  capacity-conclusion comment, and an enforced test SHALL pin that exact
  assignment line so silent drift back to a stale value fails CI

#### Scenario: Runbook records the dual-constraint derivation, not just the number

- **WHEN** the operator runbook's per-tick capacity section is read
- **THEN** it SHALL state BOTH capacity constraints — the throughput
  relation (bound × daily tick cadence versus steady-state chunk
  arrival) AND the wrapper-wall relation (the summed duration of
  selected chunks must fit the whole-tick wall, cross-referencing the
  catch-up recipe for backlog scenarios) — plus the measured inputs
  behind the current target, the derivation's invalidation conditions,
  and an explicit conclusion on timer cadence (no frequency change
  required: terminal-chunk count is time-partitioned and insensitive to
  ingest volume) so the next retune starts from the formula instead of
  incident-scene guesswork

#### Scenario: Live bound matches the target and is receipt-proven

- **WHEN** the deployed node-27 env and runner receipts are inspected
- **THEN** the env SHALL set the same bound value as the template, a
  receipt of any mode SHALL echo `per_tick_bound` equal to that value,
  and an enforce-mode receipt SHALL prove the clean outcome under that
  bound (the dry-run outcome field is constant and carries no signal)
