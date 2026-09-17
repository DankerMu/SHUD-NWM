## ADDED Requirements

### Requirement: The national discharge intersection's denominator is never older than its numerator

The helper that answers "which active river networks cover this `(source, cycle)`" SHALL read the
active-network set (the denominator) from a snapshot that is at least as fresh as the snapshot the
display-ready coverage rows (the numerator) come from. A river network activated after the coverage
rows were read and before the active set was read MUST therefore make the set comparison unequal, so
every cycle judged by that pair of reads fails closed, regardless of whether the newly active network
contributed any coverage row. Without a concurrent activation or deactivation this rule MUST NOT change any result:
the coverage rows are already filtered on `active_flag`, so the covered set is a subset of the active
set and the comparison is unchanged; the requirement is about read order alone and makes no claim
about a network deactivated between the reads. The rule applies to every consumer of that helper — the
`GET /api/v1/layers/discharge/cycles` list, the per-cycle branch of
`GET /api/v1/layers/discharge/valid-times`, and the canonical national tile route's coverage check —
because all three read the intersection through it. This requirement constrains the ORDER of the two
reads only; it does not make them atomic, and it does not govern a coverage row that ceases to be
display-ready between them.

#### Scenario: A network activated with zero coverage rows empties the cycles list

- **WHEN** the coverage rows are read while the active networks are `{rn-b, rn-c}`, network `rn-a` is
  then activated with no display-ready run at all, and the active-network set is read afterwards as
  `{rn-a, rn-b, rn-c}`
- **THEN** `GET /api/v1/layers/discharge/cycles?source=gfs` returns `cycles: []` and
  `default_cycle: null`

#### Scenario: A network activated with zero coverage rows empties the per-cycle valid times

- **WHEN** the same activation happens around the reads for one requested `(source, cycle)`
- **THEN** `GET /api/v1/layers/discharge/valid-times?source=gfs&cycle=C` returns an empty
  `valid_times` list with an observed count of `0`

#### Scenario: A network activated with coverage for only one cycle closes the other cycle too

- **WHEN** the coverage rows are read while the active networks are `{rn-b, rn-c}`, network `rn-a` is
  then activated holding a display-ready run for cycle `K` but none for the older cycle `J`, and the
  active-network set is read afterwards as `{rn-a, rn-b, rn-c}`
- **THEN** neither `K` nor `J` is listed by `GET /api/v1/layers/discharge/cycles?source=gfs`
- **AND** `GET /api/v1/layers/discharge/valid-times?source=gfs&cycle=J` returns an empty
  `valid_times` list

#### Scenario: Without a concurrent membership change every result is unchanged

- **WHEN** no network is activated or deactivated between the two reads
- **THEN** the listed cycles, their `valid_time_start` / `valid_time_end` bounds, the per-cycle valid
  times, and the canonical national tile route's coverage verdict are exactly what they were before
  this requirement
