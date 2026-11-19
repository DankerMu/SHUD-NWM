## REMOVED Requirements

### Requirement: Targeted CI selection covers every new partition

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.
