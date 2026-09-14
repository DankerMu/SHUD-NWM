## ADDED Requirements

### Requirement: Blocked-transition predecessor evidence SHALL carry the matcher's state-index identity

The `selected_predecessor` evidence SHALL carry, when a strict warm-start transition
blocks as `block_predecessor_pending` or `block_wrong_generation`, the same identity the exact-predecessor state-index lookup uses for that candidate:
`source_id`, `valid_time` equal to the candidate cycle time, `cycle_id` naming the
producing cycle at candidate cycle time minus `lead_hours`, and `lead_hours`. Any
consumer that needs the predecessor cycle time SHALL derive it from that identity and
SHALL treat evidence whose `cycle_id` disagrees with `valid_time - lead_hours` as
malformed.

#### Scenario: Pending block reports the looked-up key

- **GIVEN** a gfs candidate at 2026-07-06T12:00Z with required lead 12 h whose exact new-generation predecessor is absent
- **WHEN** the transition evaluates to `block_predecessor_pending`
- **THEN** `selected_predecessor.valid_time` is 2026-07-06T12:00:00Z and `selected_predecessor.cycle_id` is `gfs_2026070600`
- **AND** the same values are the ones the state-index exact-predecessor key is built from

#### Scenario: Wrong-generation block reports the looked-up key

- **GIVEN** an entry at the exact predecessor key carrying an old package checksum
- **WHEN** the transition evaluates to `block_wrong_generation`
- **THEN** `selected_predecessor` carries the identical `valid_time`, `cycle_id`, and `lead_hours` as the pending case

#### Scenario: Predecessor backfill emits the producing cycle

- **GIVEN** a blocked successor whose `selected_predecessor` has `valid_time` 2026-07-06T12:00Z, `lead_hours` 12, `cycle_id` `gfs_2026070600`
- **WHEN** predecessor backfill emission runs
- **THEN** the emitted predecessor candidate is for cycle 2026-07-06T00:00Z with cycle id `gfs_2026070600`

#### Scenario: Inconsistent predecessor evidence is not emitted

- **GIVEN** a blocked successor whose `selected_predecessor.cycle_id` disagrees with `valid_time - lead_hours`
- **WHEN** predecessor backfill emission runs
- **THEN** no predecessor candidate is emitted for that record

#### Scenario: Predecessor evidence without cycle id is derived

- **GIVEN** a blocked successor whose `selected_predecessor` has `valid_time` 2026-07-06T12:00Z and `lead_hours` 12 but no `cycle_id`
- **WHEN** predecessor backfill emission runs
- **THEN** the emitted predecessor candidate is for cycle 2026-07-06T00:00Z
