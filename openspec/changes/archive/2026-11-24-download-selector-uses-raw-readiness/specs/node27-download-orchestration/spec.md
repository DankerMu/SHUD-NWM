## ADDED Requirements

### Requirement: Automatic cycle selection re-drives incomplete cycles and serves the latest allowed cycle first
When no explicit cycle time is given, the node-27 download pass SHALL judge a cycle as downloaded for a source only when the raw manifest readiness rule the scheduler applies answers `ready` for that source and cycle: the manifest identity matches and every referenced raw file is a non-empty regular file. A cycle is ready when every selected source is ready for it; a cycle with at least one selected source that is not ready is a not-ready cycle. Over the window from the chain start to the latest allowed cycle, the pass SHALL select the latest allowed cycle when it is a not-ready cycle, otherwise the oldest not-ready cycle, otherwise the latest allowed cycle. The pass summary SHALL list the cycles judged not ready with the status and reason per source, bounded in length. An explicit cycle time SHALL be honoured as before.

#### Scenario: Started-but-incomplete older cycle is selected again
- **GIVEN** three consecutive allowed cycles whose manifests and files are complete for GFS and IFS
- **AND** one raw file of the middle cycle is then removed for IFS
- **WHEN** the newest of the three is the latest allowed cycle and the pass selects automatically
- **THEN** the middle cycle is selected

#### Scenario: Latest allowed cycle is served before older gaps
- **GIVEN** an older cycle that is not ready and a latest allowed cycle that is not ready
- **WHEN** the pass selects automatically
- **THEN** the latest allowed cycle is selected

#### Scenario: Oldest not-ready cycle is served once the latest is ready
- **GIVEN** a ready latest allowed cycle and two older cycles that are not ready
- **WHEN** the pass selects automatically
- **THEN** the older of the two is selected

#### Scenario: A cycle that never gets a manifest does not starve the latest
- **GIVEN** an older cycle with no manifest for one selected source and a latest allowed cycle with no manifest
- **WHEN** the pass selects automatically
- **THEN** the latest allowed cycle is selected

#### Scenario: Complete chain
- **GIVEN** every cycle from the chain start to the latest allowed cycle is ready
- **WHEN** the pass selects automatically
- **THEN** the latest allowed cycle is selected and the summary lists no not-ready cycle

#### Scenario: Not-ready cycles are reported
- **GIVEN** an older cycle that is not ready for one source
- **WHEN** the pass or the preflight-only run completes with automatic selection
- **THEN** its summary names that cycle with the source's readiness status and reason
