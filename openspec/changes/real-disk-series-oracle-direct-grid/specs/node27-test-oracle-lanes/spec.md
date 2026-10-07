## MODIFIED Requirements

### Requirement: Real-disk forcing suite derives its cycle from the live store
`tests/test_object_store_forcing_real_disk.py` SHALL select, at run time, the newest cycle for which all four basin/source combinations are present in `OBJECT_STORE_ROOT`. A combination SHALL be resolved per cycle from the store and the database, not named in the suite: its model is the single `dg_*` directory of that basin version and source in the cycle, and its station is one that `met.interp_weight` holds for that model and whose forcing file exists in the cycle. Every time the suite asserts SHALL be derived from that cycle. When no such cycle exists, the suite SHALL fail with an explicit diagnostic rather than skip.

#### Scenario: Newest complete cycle is chosen
- **WHEN** the store holds cycles where the newest is missing one combination
- **THEN** the suite uses the newest cycle that has all four combinations

#### Scenario: No complete cycle
- **WHEN** no cycle has all four combinations
- **THEN** the suite fails with a message naming the store root, the per-combination counts and why each combination was absent

#### Scenario: A combination that cannot be resolved without guessing
- **WHEN** a cycle holds no `dg_*` directory, or more than one, for a basin version and source
- **THEN** that combination is absent for that cycle and the suite does not pick one of the directories
