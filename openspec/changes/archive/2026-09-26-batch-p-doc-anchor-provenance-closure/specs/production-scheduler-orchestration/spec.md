## ADDED Requirements

### Requirement: Additional retention roots are discarded by value shape

The retention additional-root resolver SHALL discard a `None`, empty or whitespace-only value silently, recording no skip entry. It SHALL discard a non-blank relative value loudly, with a skip entry that uses the not-absolute reason. It SHALL admit an absolute value. The resolver SHALL base this decision only on the value's shape, because it cannot see the deployment topology. This refines the existing requirement "Retention covers every configured run-workspace root".

#### Scenario: Unset and blank values are silent

- **WHEN** the configured additional roots are `None`, `""` and `"   "`
- **THEN** no root is resolved and no skip entry is recorded

#### Scenario: A relative value is recorded while an absolute one is admitted

- **WHEN** the configured additional roots are a relative path and an absolute path
- **THEN** the relative path produces one not-absolute skip entry and the absolute path is admitted
