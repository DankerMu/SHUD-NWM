## ADDED Requirements

### Requirement: Production-state polling tests MUST distinguish unexpected causes from expected state outcomes

Each issue-owned pytest test that starts or observes a worker and polls production state (`heartbeat_seq`, `lost`, or a terminal-intent path) SHALL surface an unexpected cause observable at that test boundary before asserting worker-produced state or result collections. Expected production outcomes SHALL remain distinct: `_LeaseHeartbeat._run` SHALL continue mapping `renew()` exceptions to fail-closed `lost=True`; a real stolen-token renewal returning `False` SHALL remain the takeover oracle; and an expected terminal-intent `TerminalStateError` SHALL remain a substantive reader result. Captured-failure paths SHALL retain the existing poll/join bounds and complete test-owned thread and file-lock cleanup before the parent reports the cause.

This requirement governs only `test_lease_heartbeat_advances_then_detects_takeover` in `tests/test_production_scheduler.py` and `test_shared_finalizer_and_authoritative_reader_complete_without_deadlock` in `tests/test_node27_timeseries_compression_supervisor.py`, plus regression tests that exercise those exact shipping functions. It does not reclassify production-state polls as #1633 dedicated completion sentinels, change production modules or state semantics, or replace repository-wide warning/timeout policy tracked by #1646.

#### Scenario: Heartbeat renewal exception precedes sequence symptoms

- **WHEN** the real heartbeat calls the test-observed `renew()` boundary and it raises before the first successful sequence increment
- **THEN** production maps the failure to `lost=True`, while the shipping test reports the captured exception identity before asserting `heartbeat_seq`
- **AND** the bounded poll and heartbeat stop/join cleanup still complete

#### Scenario: Exception cannot impersonate a stolen-token takeover

- **WHEN** one real renewal succeeds, the test replaces the lease token, and a later observed renewal raises instead of returning `False`
- **THEN** the shipping test reports that exception before accepting `lost=True`
- **AND** in the unchanged normal path the real token mismatch returns `False`, `lost=True` remains the substantive takeover result, and no captured exception exists

#### Scenario: Production exception-to-lost mapping remains fail-closed

- **WHEN** `FileSchedulerLease.renew()` raises an `Exception` in a direct `_LeaseHeartbeat` control
- **THEN** `_LeaseHeartbeat._run` catches it and sets `lost=True` without requiring the exception to escape the production daemon
- **AND** no production heartbeat, lease, scheduler-runtime, or state contract is changed

#### Scenario: Unexpected finalizer or reader failure is cause-first

- **WHEN** the issue-owned supervisor test's finalizer or reader worker raises an unexpected `BaseException`
- **THEN** the parent reports the injected cause before asserting `finalizer_result` or `reader_result`
- **AND** both workers are bounded-joined and the parent-owned file lock is released and closed before that failure surfaces

#### Scenario: Expected terminal-intent error remains a result

- **WHEN** the finalizer records pending intent while the parent holds the terminal lock and the authoritative reader encounters the expected `TerminalStateError`
- **THEN** the finalizer result remains `False`, the expected error remains in `reader_result`, and subsequent final receipt publication still succeeds
- **AND** the expected domain error is not placed in the unexpected-worker-error channel

#### Scenario: Exact shipping owners prove attribution

- **WHEN** tracked regressions inject unique failures into first renewal, post-takeover renewal, finalizer, and reader call sites
- **THEN** each injected callable is proven to execute and direct invocation of the corresponding shipping test reports its unique cause
- **AND** removing only the shipping test's cause-observation leg restores a state symptom, a false-green, or a warning-only failure, so copied toy harnesses cannot satisfy the evidence

#### Scenario: Adjacent global policy remains separate

- **WHEN** repository-wide `PytestUnhandledThreadExceptionWarning`, pytest timeout, or cancellation policy is considered
- **THEN** this change does not edit that policy, dependency, or configuration because #1646 owns the shape-independent backstop
