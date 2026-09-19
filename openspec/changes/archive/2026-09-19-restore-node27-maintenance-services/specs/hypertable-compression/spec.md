## MODIFIED Requirements

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
existing `per_tick_bound` field. The variable remains mandatory in the
compression env (an env without it is refused); the in-code default, equal to
the target, serves only direct unassembled invocations that read no env.

#### Scenario: Template carries the pinned capacity target

- **WHEN** `infra/env/node27-timeseries-compression.example` is read
- **THEN** it SHALL contain the uncommented line
  `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=2` with a
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


## ADDED Requirements

### Requirement: Compression selection SHALL walk each hypertable newest-eligible-first

The compression runner SHALL order eligible chunks (uncompressed, `range_end`
before `now − lag`) hypertable by hypertable in the catalog query's
`(hypertable_schema, hypertable_name)` order, and within one hypertable by
`range_end` descending, and SHALL select the first `per_tick_bound` chunks of
that sequence. Deferred chunks SHALL follow the same sequence; chunks inside
the lag window SHALL keep the catalog order. The resulting receipt order SHALL
be deterministic for a given catalog. This keeps compression work on the
chunks with the longest remaining life instead of on the chunks the DB
retention lane drops next, because both lanes derive their cutoffs from the
same display watermark and retention's eligible set is the oldest prefix of
compression's.

#### Scenario: compression's oldest end overlaps retention's drop set

- **WHEN** one hypertable holds eligible chunks whose `range_end` spans both
  sides of the retention cutoff `W − 21 d`, with at least `per_tick_bound`
  eligible chunks younger than that cutoff
- **THEN** every selected chunk has `range_end` later than `W − 21 d`
- **AND** none of the selected chunks is in the set the retention runner would
  drop on the same watermark

#### Scenario: table order is preserved across hypertables

- **WHEN** `hydro.river_timeseries` has at least `per_tick_bound` eligible
  chunks and `hydro.river_timeseries_legacy` has older eligible chunks
- **THEN** every selected chunk belongs to `hydro.river_timeseries`
- **AND** the selected chunks are that table's newest eligible chunks in
  `range_end` descending order

#### Scenario: a free slot moves to the next hypertable

- **WHEN** `hydro.river_timeseries` has fewer eligible chunks than
  `per_tick_bound`
- **THEN** the remaining slots go to the next hypertable in
  `(schema, name)` order, newest eligible first
