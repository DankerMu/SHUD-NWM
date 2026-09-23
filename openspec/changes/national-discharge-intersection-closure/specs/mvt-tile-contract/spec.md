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
