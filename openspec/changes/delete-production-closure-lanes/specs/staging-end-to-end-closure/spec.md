## REMOVED Requirements

### Requirement: Staging forecast chain evidence closes from source to API

**Reason**: The staging end-to-end closure (`validate-e2e`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.

### Requirement: SHUD output QC blocks unsafe publication

**Reason**: The staging end-to-end closure (`validate-e2e`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None for the closure evidence; malformed-output rejection and QC in the live pipeline stay specified by `output-parsing`.

### Requirement: Frontend smoke evidence preserves staging lineage

**Reason**: The staging end-to-end closure (`validate-e2e`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.

### Requirement: Closure evidence bundle is durable

**Reason**: The staging end-to-end closure (`validate-e2e`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.
