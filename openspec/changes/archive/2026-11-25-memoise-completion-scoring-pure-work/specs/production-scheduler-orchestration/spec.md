## ADDED Requirements

### Requirement: Completion scoring SHALL NOT repeat pure work per model
Cycle completion scoring SHALL obtain each model's public candidate state with the same content as before, while work whose result depends on a single string or on the source and cycle alone is done once and reused: the public sanitiser SHALL return, for every input, exactly what it returns without reuse, and the raw-manifest readiness of a source cycle SHALL be computed once while that cycle is scored. Reuse SHALL be bounded in memory and SHALL NOT extend the readiness result beyond the scoring of one cycle.

#### Scenario: The sanitiser's output does not depend on reuse
- **WHEN** a payload containing secrets, absolute paths, URIs and free text is rendered for public evidence twice, and once more after the remembered results are discarded
- **THEN** the three renderings are equal
- **AND** each equals the rendering of the unmemoised sanitiser

#### Scenario: Remembered sanitiser results are bounded
- **WHEN** more distinct strings are sanitised than the cache holds, or a string longer than the cached length limit is sanitised
- **THEN** the number of remembered results never exceeds the configured maximum
- **AND** the over-long string is sanitised correctly without being remembered

#### Scenario: Readiness is computed once per scored cycle
- **WHEN** a cycle with several in-scope models is scored
- **THEN** the raw-manifest readiness of that source cycle is computed at most once, and exactly once when candidate states are read
- **AND** every model's candidate state carries a readiness mapping equal to the one computed without reuse

#### Scenario: Each state owns its readiness mapping
- **WHEN** one candidate state's readiness mapping is modified after it was handed out
- **THEN** the mapping handed to the next model of the same cycle is unaffected

#### Scenario: Reuse ends with the cycle
- **WHEN** scoring of a cycle has finished and readiness is asked again, by the next cycle's scoring or by any caller outside completion scoring
- **THEN** it is computed again from the filesystem

#### Scenario: A failed readiness computation is not reused
- **WHEN** computing readiness raises while a cycle is scored
- **THEN** the error reaches the caller as before
- **AND** the next request in the same scoring computes readiness again
