## REMOVED Requirements

### Requirement: Shared source retry preserves runtime roots

**Reason**: Specifies manual `download_source_cycle` retry submission manifests, pre-submit fail-closed root resolution and `submission_failed` transitions, which exist only in the retired lanes (`RetryService._resolve_retry_runtime_roots`, file `attempt_manual_retry` -> `_resolve_file_retry_runtime_roots`).

**Migration**: None; the capability has no surviving requirement. Scheduler-side shared source-cycle submissions are specified in the scheduler/chain capabilities.

### Requirement: Shared source retry fails closed without required roots

**Reason**: Specifies manual `download_source_cycle` retry submission manifests, pre-submit fail-closed root resolution and `submission_failed` transitions, which exist only in the retired lanes (`RetryService._resolve_retry_runtime_roots`, file `attempt_manual_retry` -> `_resolve_file_retry_runtime_roots`).

**Migration**: None; the capability has no surviving requirement. Scheduler-side shared source-cycle submissions are specified in the scheduler/chain capabilities.

### Requirement: Runtime-root evidence is auditable and redacted

**Reason**: Specifies manual `download_source_cycle` retry submission manifests, pre-submit fail-closed root resolution and `submission_failed` transitions, which exist only in the retired lanes (`RetryService._resolve_retry_runtime_roots`, file `attempt_manual_retry` -> `_resolve_file_retry_runtime_roots`).

**Migration**: None; the capability has no surviving requirement. Scheduler-side shared source-cycle submissions are specified in the scheduler/chain capabilities.

### Requirement: Existing retry compatibility is preserved

**Reason**: Both scenarios ("Non-source job retry", "Duplicate active retry") guard the retired manual retry submission manifest and duplicate-manual-retry rejection.

**Migration**: None; the capability has no surviving requirement. Scheduler-side shared source-cycle submissions are specified in the scheduler/chain capabilities.

