## REMOVED Requirements

### Requirement: Object-store package closure extends M9 publication to production-like storage

**Reason**: The production object-store (`validate-object-store`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None; Basins package publication itself stays specified by `shud-model-package-publication`.

### Requirement: Runtime staging prefix open releases its descriptor on every rejection

**Reason**: The production object-store (`validate-object-store`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.
