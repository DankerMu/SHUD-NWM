## REMOVED Requirements

### Requirement: Production configuration is validated before release

**Reason**: The production ops (`validate-ops`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.
