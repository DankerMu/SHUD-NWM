## ADDED Requirements

### Requirement: Forcing usability of canonical quality flags

Canonical readiness (`evaluate_canonical_readiness`) and the forcing producer's canonical-product selection (`canonical_product_is_forcing_usable`, used by the file-backed DB-free path) SHALL treat a product as forcing-usable when it has a non-empty checksum and its `quality_flag` is `ok`, `warn`, or `warning_negative_precip`. A precipitation `warning_negative_precip` product is diagnostic only: its values are already clamped to zero. The flag `error_precip_accumulation`, and any other flag, SHALL make the product unusable. Readiness evidence SHALL report accepted non-`ok` flags as `accepted_warning_quality_flags` (flag to row count), so that accepting a warning is auditable. The DB-backed selection queries (`workers/forcing_producer/store.py`, `services/orchestrator/chain_repository.py`), which already accept only `ok`, are outside this requirement.

#### Scenario: Single negative-precipitation warning no longer blocks the cycle

- **WHEN** an IFS cycle is evaluated against its expected forecast hours, every required canonical product is present with a checksum, and exactly one precipitation lead is flagged `warning_negative_precip`
- **THEN** canonical readiness reports the cycle ready, and `accepted_warning_quality_flags` equals `{"warning_negative_precip": 1}`

#### Scenario: Accumulated precipitation error still blocks the cycle

- **WHEN** the same cycle has that lead flagged `error_precip_accumulation` instead
- **THEN** canonical readiness reports the cycle not ready with reason `missing_canonical_leads`, and `rejected_quality_flags` contains `error_precip_accumulation`
