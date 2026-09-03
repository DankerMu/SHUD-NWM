## MODIFIED Requirements

### Requirement: The compression per-tick bound MUST be a capacity-derived target consistent across template, live env, and receipts

The per-tick bound SHALL be a decided capacity target derived from
measured inputs (the chunk time interval of every governed hypertable
and the resulting steady-state terminal-chunk arrival rate — one chunk
per canonical hypertable per interval, so one-day chunks on the two
canonical tables arrive at two per day, while a transitional `_legacy`
sibling is a write-frozen finite backlog of seven-day chunks with zero
steady-state arrival and is modelled as such; the retention-window
backlog ceiling; and the relation between observed per-chunk
compression duration and the wrapper wall — the wall bounds the WHOLE
tick, so the bound is a throughput ceiling, not a redeemable single-tick
capacity, and the worst mixed tick of one legacy seven-day chunk plus
narrow one-day chunks MUST be measured and stated; catch-up under
backlog follows the runbook's catch-up recipe rather than relying on
the bound), not an arbitrary default or an unrecorded live retune: the
committed env template SHALL carry the target value with a comment
identifying it as a capacity conclusion and pointing at the recorded
derivation in the operator runbook, the deployed node-27 env SHALL carry
the same value, and every receipt echoes the effective bound via its
existing `per_tick_bound` field. The variable remains mandatory with no
in-code default. A change of any governed hypertable's chunk time
interval SHALL invalidate the derivation and require the template,
runbook and live env to be re-pinned in the same change.

#### Scenario: Template carries the pinned capacity target

- **WHEN** `infra/env/node27-timeseries-compression.example` is read
- **THEN** it SHALL contain the uncommented line
  `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=4` with a
  capacity-conclusion comment re-derived for one-day chunks (two
  canonical arrivals per day under a 4-chunk bound; 4 × ≈7.5 min narrow
  chunks inside the 65-minute wrapper wall), and an enforced test SHALL
  pin that exact assignment line so silent drift back to a stale value
  fails CI

#### Scenario: Template lag assignment matches the live lag

- **WHEN** the same template is read
- **THEN** its uncommented `NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS`
  assignment equals the live two-day value (172800) and no comment
  describes the lag as "one chunk width"

#### Scenario: Runbook records the dual-constraint derivation, not just the number

- **WHEN** the operator runbook's per-tick capacity section is read
- **THEN** it SHALL state BOTH capacity constraints — the throughput
  relation (bound × daily tick cadence versus steady-state chunk
  arrival, with the chunk interval of each governed hypertable as an
  explicit input and the legacy sibling modelled as a finite backlog)
  AND the wrapper-wall relation (the summed duration of selected chunks
  must fit the whole-tick wall, including the measured worst mixed
  legacy-plus-narrow tick, cross-referencing the catch-up recipe and the
  pre-expand "compress the legacy backlog first under bound 1" recipe)
  — plus the measured inputs behind the current target, the
  derivation's invalidation conditions (chunk-interval change listed
  among them), and an explicit conclusion on timer cadence (one-day
  chunks raise arrival from two per week to two per day; the daily tick
  still covers it under bound 4, so no frequency change is required) so
  the next retune starts from the formula instead of incident-scene
  guesswork

#### Scenario: Live bound matches the target and is receipt-proven

- **WHEN** the deployed node-27 env and runner receipts are inspected
- **THEN** the env SHALL set the same bound value as the template, a
  receipt of any mode SHALL echo `per_tick_bound` equal to that value,
  and an enforce-mode receipt SHALL prove the clean outcome under that
  bound (the dry-run outcome field is constant and carries no signal)

#### Scenario: Chunk interval change without re-derivation is caught

- **WHEN** a migration changes a governed hypertable's chunk time interval
  and the template's bound comment still cites the previous interval
- **THEN** the enforced template test fails naming the stale derivation
