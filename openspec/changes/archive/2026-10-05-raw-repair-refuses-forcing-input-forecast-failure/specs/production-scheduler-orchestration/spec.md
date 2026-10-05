## ADDED Requirements

### Requirement: Raw-manifest repair retries refuse a forcing-input forecast failure

The raw-manifest repair channel and the downstream-retry-after-raw-repair channel SHALL NOT grant an automatic retry when the candidate's failure is permanent, at a native SHUD stage, with a forcing-input error code. Re-ingesting raw input cannot repair a forcing package the runtime rejected. Such a candidate SHALL fall through to the remaining ladder and be blocked by the permanent-failure guard. Every other failure these channels accept today SHALL keep its retry.

#### Scenario: Forcing-input forecast failure after a raw-manifest repair is blocked

- **WHEN** a candidate's recorded failure is at `forecast` with a forcing-input error code such as `FORCING_PACKAGE_CHECKSUM_MISMATCH`, its forcing package is witnessed present, and the raw manifest was repaired by a later successful download
- **THEN** the scheduler decision is `blocked` by the permanent-failure guard on both the default lane and the strict warm-start lane
- **AND** no retry evidence with reason `retry_downstream_after_raw_repair` is produced

#### Scenario: Forcing-input forecast failure with a missing raw manifest is blocked

- **WHEN** a candidate's recorded failure is at `forecast` with a forcing-input error code, its forcing package is witnessed present, a download stage succeeded, and the raw manifest is probed missing
- **THEN** the scheduler decision is `blocked` by the permanent-failure guard on both lanes, no `repair_missing_raw_manifest` retry is produced, and the raw manifest stays missing until an operator manual retry

#### Scenario: Other failures keep the raw-repair retry

- **WHEN** the same candidate's error code is not a forcing-input code, or its failed stage is not a native SHUD stage
- **THEN** the raw-repair channel's retry decision is unchanged
