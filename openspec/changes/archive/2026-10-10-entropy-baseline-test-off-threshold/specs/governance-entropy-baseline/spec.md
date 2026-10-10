## ADDED Requirements

### Requirement: The current-repo cleanup-priorities test SHALL NOT pin a threshold-derived impact or ordering as a literal

The test that asserts the entropy baseline writer's cleanup priorities for the current repository SHALL pin the entries and fields that are stable by construction, and SHALL NOT pin as a literal a cleanup-priority impact or ordering that flips with the live findings; the threshold and the ordering rule SHALL be pinned by a test over synthetic findings.

#### Scenario: A finding count crosses the impact threshold

- **WHEN** the number of broad-API-mock findings in the repository moves from below the impact threshold to at or above it
- **THEN** the current-repo cleanup-priorities test still passes, and the synthetic-findings test shows the impact and the ordering changing at the threshold
