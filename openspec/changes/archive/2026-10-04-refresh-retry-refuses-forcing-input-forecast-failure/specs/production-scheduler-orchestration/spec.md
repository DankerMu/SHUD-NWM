## ADDED Requirements

### Requirement: Model-package refresh retry refuses a forcing-input forecast failure

The model-package refresh retry channel SHALL NOT grant an automatic retry when the candidate failed at `forecast` with a forcing-input error code (failed stage in the native SHUD stage aliases, the condition under which the channel restarts at `forecast`). A changed model package cannot repair a rejected forcing package, and a restart at `forecast` re-stages the same package. Such a candidate SHALL fall through to the permanent-failure guard. Every other failure the channel accepts today SHALL keep its refresh retry.

#### Scenario: Forcing-input forecast failure with a changed model package is blocked

- **WHEN** a candidate's recorded failure is permanent, its failed stage is one of the native SHUD stage aliases that this channel restarts at `forecast`, its forcing package is witnessed present, its error code is a forcing-input code such as `FORCING_PACKAGE_CHECKSUM_MISMATCH`, and `run_manifest_model_package` differs from the candidate's model package
- **THEN** the scheduler decision is `blocked` by the permanent-failure guard, on both the default lane and the strict warm-start lane
- **AND** no evidence with `restart_stage` equal to `forecast` and `automatic_retry_allowed` true is produced

#### Scenario: Other forecast failures keep the refresh retry

- **WHEN** the same candidate's error code is not a forcing-input code, for example `SHUD_FAILED` or `INVALID_MANIFEST`
- **THEN** the decision is `retry` with `restart_stage` equal to `forecast`; the reason is `retry_after_model_package_refresh` on the default lane and may be rewritten by the strict warm-start escalator on the strict lane
