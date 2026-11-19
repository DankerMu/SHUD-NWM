## REMOVED Requirements

### Requirement: National performance evidence

**Reason**: The national tile performance evidence was produced only by the national-scale (`validate-scale`) validation lane, which is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None; the live MVT contract stays specified by `mvt-tile-contract`.
