## REMOVED Requirements

### Requirement: Staging forecast chain evidence closes from source to API

**Reason**: The staging end-to-end closure (`validate-e2e`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.

### Requirement: SHUD output QC blocks unsafe publication

**Reason**: The staging end-to-end closure (`validate-e2e`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None for the closure evidence; rejection of non-finite SHUD output in the live pipeline stays pinned by `tests/test_output_parser.py` (`NON_FINITE_FLOW`, `workers/output_parser/parser.py`).

### Requirement: Frontend smoke evidence preserves staging lineage

**Reason**: The staging end-to-end closure (`validate-e2e`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.

### Requirement: Closure evidence bundle is durable

**Reason**: The staging end-to-end closure (`validate-e2e`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.
