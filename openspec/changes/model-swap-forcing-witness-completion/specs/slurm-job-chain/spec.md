# slurm-job-chain — delta

## ADDED Requirements

### Requirement: Cycle-stage retry attempt numbering SHALL use the last retry suffix

The cycle executor SHALL derive the next retry attempt for a stage from existing
job rows (when no explicit context retry attempt is set) as follows: each matching row
whose job id starts with the stage base id followed by `_retry_` SHALL contribute the attempt
encoded in its LAST `_retry_<n>` suffix, and the next attempt SHALL be the
maximum contribution plus one. A row whose last suffix does not parse SHALL
contribute nothing, regardless of any recorded retry count. An explicit context
retry attempt SHALL keep precedence, and the derivation SHALL keep using the
same jobs snapshot that selected the stage row.

#### Scenario: Stacked retry ids number past the last suffix

- **WHEN** the rows for a stage base `B` include `B_retry_1` and a selected
  terminal `B_retry_1_retry_2_retry_3`, and no context retry attempt is set
- **THEN** the minted retry id SHALL be `B_retry_4`, and the reservation key and
  gateway comment SHALL carry `retry_4`

#### Scenario: Flat suffixes and malformed tails are unchanged

- **WHEN** the rows include `B_retry_1`, `B_retry_2`, and `B_retry_garbage`
- **THEN** the next attempt SHALL be 3
