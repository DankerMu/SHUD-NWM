## ADDED Requirements

### Requirement: Stage-less forcing-input manual retry stays full-chain on the strict lane

When a manual retry has no restart stage and its state evidence carries the forcing-input failure marker, the strict warm-start upgrader's result SHALL NOT replace it: the decision stays a full-chain retry and the next pass submits convert, forcing and forecast. Retries without the marker SHALL keep the existing upgrade and fallback behaviour. A limited checksum read of a missing or unreadable forcing object SHALL report a forcing-family error code that the forcing-input matcher recognises, and SHALL report the limit code only for a real limit violation.

#### Scenario: Forcing package failure before the run manifest reaches the object store
- **WHEN** a forecast failed with `FORCING_PACKAGE_CHECKSUM_MISMATCH`, no `input/manifest.json` exists in the object store, strict warm start is required and the operator marks a manual retry
- **THEN** the decision has no restart stage, its reason is not `strict_warm_start_retry_run_manifest_mismatch`, and convert, forcing and forecast are submitted

#### Scenario: Missing direct-grid tsd.forc member
- **WHEN** the direct-grid package lacks its `.tsd.forc` member
- **THEN** the runtime error code is not `DIRECT_GRID_TSD_FORC_TOO_LARGE` and the forcing-input matcher returns true
