## REMOVED Requirements

### Requirement: OUTPUT_UNITS and manifest-unit keysets stay in lockstep

**Reason**: `REQUIRED_FORCING_VARIABLES` and `package_manifest_unit` exist only in the production met (`validate-met`) validation lane, which is deleted (#3221) together with its guard test: it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.
