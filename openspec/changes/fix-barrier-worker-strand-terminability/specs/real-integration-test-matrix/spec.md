## ADDED Requirements

### Requirement: Barrier concurrency harnesses MUST fail bounded and leave no stranded peer

Every issue-owned pytest harness that starts multiple workers whose progress depends on all participants reaching a `threading.Barrier` SHALL bound that Barrier protocol independently of successful worker progress. If a participant raises or never arrives, every waiting peer SHALL leave the barrier within the bound, the parent SHALL observe and attribute worker/future failures before asserting race output, and every started worker SHALL terminate before the test returns so `threading._shutdown()` cannot hang the pytest process. Conformance SHALL preserve the harness's participant count, real code-under-test calls, race window, and substantive concurrency assertions.

This requirement initially governs the four sites explicitly routed by #1645: the concurrent idempotency reservation and file-submit collision tests in `tests/test_gateway_reconcile.py`, and the thread-lock serialization and receipt-retention tests in `tests/test_scheduler_file_provider_refresh.py`. It does not make a repository-wide claim about unrelated Barrier sites and does not replace global warning/timeout policy tracked by #1646.

#### Scenario: All participants arrive and the original race oracle is unchanged

- **WHEN** every participant reaches a governed Barrier and the code under test succeeds
- **THEN** the Barrier releases the same participant population into the same real concurrent operation
- **AND** the original winner/loser, serialization, state, retention, and result assertions remain unchanged and pass

#### Scenario: A participant raises before Barrier arrival

- **WHEN** one governed worker raises before reaching the Barrier
- **THEN** waiting peers leave with a bounded broken-barrier outcome rather than remaining stranded
- **AND** the parent reports the injected worker exception and peer failure before any missing-result or state assertion
- **AND** every started worker/future is joined or consumed before the test returns

#### Scenario: The whole pytest process remains terminable

- **WHEN** a bounded subprocess drives a governed harness through a missing-participant/pre-arrival-failure path
- **THEN** the subprocess exits before its external deadline after reporting the failure
- **AND** a mutant that removes the Barrier bound or restores the strand shape hits the external deadline and is killed/reaped, proving the terminability oracle is load-bearing

#### Scenario: Boundedness is not a performance assertion

- **WHEN** a normal run executes on a loaded CI runner
- **THEN** the configured Barrier/join bound is a generous hang backstop rather than a tight duration SLA
- **AND** controlled injection tests may pass an explicitly shorter bound to remain fast

#### Scenario: Adjacent global policy remains separate

- **WHEN** repository-wide handling of `PytestUnhandledThreadExceptionWarning` or a global pytest timeout is considered
- **THEN** this change does not alter that policy, dependency, or configuration because #1646 owns the shape-independent decision
