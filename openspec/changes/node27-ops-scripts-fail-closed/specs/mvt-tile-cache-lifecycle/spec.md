## ADDED Requirements

### Requirement: Each MVT cache retention run SHALL leave its own summary file

When no explicit summary path is configured, the MVT cache retention wrapper SHALL write each run's summary to a path no other run uses, creating it exclusively, so that two runs starting within the same second leave two summary files.

#### Scenario: Plan-only then production in the same second

- **GIVEN** two runs of the wrapper whose clock reads the same second
- **WHEN** both complete
- **THEN** the log directory contains two distinct summary files, each valid JSON describing its own run
