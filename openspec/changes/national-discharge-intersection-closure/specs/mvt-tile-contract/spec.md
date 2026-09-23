## ADDED Requirements

### Requirement: The no-argument national valid-times list fails closed on an incomplete intersection

`GET /api/v1/layers/discharge/valid-times` called with no `run_id`, no `source` and no `cycle` SHALL intersect each active river network's newest display-ready run only when the SET of networks holding a display-ready run EQUALS the set of active river networks, where the active set is read from `core.model_instance` (`active_flag` and a non-null `river_network_version_id`) by the same helper the per-cycle branch uses — never derived from the coverage rows. When the two sets differ in any direction (an active network without a display-ready run, equal size with different members, or a covering network no longer active), the branch MUST return an empty `valid_times` list with an observed count of `0`. When the sets are equal the result MUST be unchanged, including for a network whose newest display-ready run predates the `/cycles` lookback window. This branch is a fourth consumer of the helper governed by "The national discharge intersection's denominator is never older than its numerator": its verdict under a race between the two reads follows that requirement's read order, and its residuals (deactivation, a coverage row ceasing to be display-ready) are the ones that requirement disclaims.

#### Scenario: An active network without a display-ready run empties the list

- **WHEN** the active networks are `{rn-a, rn-b, rn-c}` and only `rn-a` and `rn-b` hold display-ready runs
- **THEN** the no-argument request returns `valid_times: []` with `observed_count = 0`

#### Scenario: Equal-size membership mismatch empties the list

- **WHEN** the active networks are `{rn-b, rn-c1, rn-c2}` and display-ready runs exist for `{rn-a, rn-c1, rn-c2}`
- **THEN** the no-argument request returns `valid_times: []` with `observed_count = 0`

#### Scenario: A covering network that is no longer active empties the list

- **WHEN** display-ready runs are read for `{rn-a, rn-b, rn-c}` and the active set is read as `{rn-a, rn-b}`
- **THEN** the no-argument request returns `valid_times: []` with `observed_count = 0`

#### Scenario: A complete intersection is unchanged

- **WHEN** every active network holds a display-ready run, including one whose newest run predates the `/cycles` lookback window
- **THEN** the no-argument request returns the same `valid_times`, `observed_count` and `truncated` as before this requirement

#### Scenario: No active network and no coverage row returns an empty list

- **WHEN** there are no active networks and no display-ready runs
- **THEN** the no-argument request returns `valid_times: []` with `observed_count = 0` and does not error

## MODIFIED Requirements

### Requirement: The national discharge intersection's denominator is never older than its numerator

The helper that answers "which active river networks cover this `(source, cycle)`" SHALL read the
active-network set (the denominator) from a snapshot that is at least as fresh as the snapshot the
display-ready coverage rows (the numerator) come from. A river network activated after the coverage
rows were read and before the active set was read MUST therefore make the set comparison unequal, so
every cycle judged by that pair of reads fails closed, regardless of whether the newly active network
contributed any coverage row. Without any concurrent write to `core.model_instance`, `hydro.hydro_run` or
`hydro.run_display_coverage` between the two reads, this rule MUST NOT change any result: the coverage
rows are already filtered on `active_flag`, so the covered set is a subset of the active set and the
comparison is unchanged. The rule is about read ORDER alone. It makes no claim about a network
deactivated between the reads, and none about a coverage row that ceases to be display-ready between
them — that case is a known fail-open residual of reading the numerator first, not something this
requirement governs. The rule applies to every consumer of that helper — the
`GET /api/v1/layers/discharge/cycles` list, the per-cycle branch of
`GET /api/v1/layers/discharge/valid-times`, the no-argument branch of
`GET /api/v1/layers/discharge/valid-times`, and the canonical national tile route's coverage check —
because all four read the intersection through it. This requirement constrains the ORDER of the two
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

#### Scenario: Without a concurrent write every result is unchanged

- **WHEN** no write to `core.model_instance`, `hydro.hydro_run` or `hydro.run_display_coverage` lands
  between the two reads
- **THEN** the listed cycles, their `valid_time_start` / `valid_time_end` bounds, the per-cycle valid
  times, and the canonical national tile route's coverage verdict are exactly what they were before
  this requirement
