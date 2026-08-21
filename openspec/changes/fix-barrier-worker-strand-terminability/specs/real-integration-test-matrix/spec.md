## ADDED Requirements

### Requirement: Barrier concurrency harnesses MUST fail bounded after pre-arrival failure

Every issue-owned pytest harness that starts multiple workers whose progress depends on all participants reaching a `threading.Barrier` SHALL bound that Barrier protocol independently of successful worker progress. If a worker raises before Barrier arrival, or worker launch fails after only a subset has started, the harness SHALL abort or otherwise break the Barrier so every successfully started waiting peer leaves within the bound. The parent SHALL observe and attribute worker, Future, launch, and cleanup failures before asserting race output; every successfully started peer in this governed failure path SHALL terminate; and every returned Future SHALL be consumed before the failure leaves the harness. Conformance SHALL preserve the harness's participant count, real code-under-test calls, race window, and substantive concurrency assertions.

This requirement initially governs the four sites explicitly routed by #1645: the concurrent idempotency reservation and file-submit collision tests in `tests/test_gateway_reconcile.py`, and the thread-lock serialization and receipt-retention tests in `tests/test_scheduler_file_provider_refresh.py`. It does not require an in-process test harness to cancel an already-started worker that blocks indefinitely outside the Barrier, does not make a repository-wide claim about unrelated Barrier sites, and does not replace global warning/timeout policy tracked by #1646.

#### Scenario: All participants arrive and the original race oracle is unchanged

- **WHEN** every participant reaches a governed Barrier and the code under test succeeds
- **THEN** the Barrier releases the same participant population into the same real concurrent operation
- **AND** the original winner/loser, serialization, state, retention, and result assertions remain unchanged and pass

#### Scenario: A participant raises before Barrier arrival

- **WHEN** one governed worker raises before reaching the Barrier
- **THEN** waiting peers leave with a bounded broken-barrier outcome rather than remaining stranded
- **AND** the parent reports the injected worker exception, peer failures, and any cleanup failure before any missing-result or state assertion
- **AND** every successfully started worker is joined and every returned Future is consumed before the failure leaves the harness

#### Scenario: Worker launch fails after partial success

- **WHEN** explicit-thread launch or executor submission raises after at least one peer or Future has started
- **THEN** the harness aborts the Barrier and joins or drains all successfully started peers or returned Futures under one cleanup deadline
- **AND** the original launch exception propagates after cleanup instead of being masked by broken-barrier or downstream result errors

#### Scenario: The whole pytest process remains terminable

- **WHEN** a bounded subprocess drives a governed harness through a pre-arrival worker-exception path
- **THEN** the subprocess exits before its external deadline after reporting the failure
- **AND** a mutant that removes the Barrier bound or restores the strand shape reaches and flushes its post-readiness failure checkpoint, then hits the external deadline and is killed/reaped, proving the terminability oracle is load-bearing rather than an unrelated startup timeout

#### Scenario: Boundedness is not a performance assertion

- **WHEN** a normal run executes on a loaded CI runner
- **THEN** the configured Barrier/join bound is a generous hang backstop rather than a tight duration SLA
- **AND** controlled injection tests may pass an explicitly shorter bound to remain fast

#### Scenario: Adjacent global policy remains separate

- **WHEN** repository-wide handling of `PytestUnhandledThreadExceptionWarning` or a global pytest timeout is considered
- **THEN** this change does not alter that policy, dependency, or configuration because #1646 owns the shape-independent decision
