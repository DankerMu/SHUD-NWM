## REMOVED Requirements

### Requirement: Repository pytest MUST fail on unhandled worker-thread exceptions

**Reason**: Replaced by "Repository pytest MUST treat unhandled worker-thread exceptions as errors": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Repository pytest MUST treat unhandled worker-thread exceptions as errors"; apart from the selector cuts its body and scenarios are unchanged.

## ADDED Requirements

### Requirement: Repository pytest MUST treat unhandled worker-thread exceptions as errors

Repository pytest configuration SHALL treat `pytest.PytestUnhandledThreadExceptionWarning` as an error. The policy SHALL target that exact category, preserve unrelated warning behavior, retain the worker's original cause in failure output, and execute in local, full, and real-integration pytest lanes installed from the repository development dependencies.

This repository-wide boundary is defense in depth. It SHALL NOT replace harness-owned exception capture, cause-before-result ordering, peer release, bounded joins/waits, cleanup, or whole-process terminability proof where an issue-specific harness contract requires those stronger outcomes.

#### Scenario: Unhandled worker exception fails with its cause

- **WHEN** a throwaway test starts and joins a worker that raises a unique `RuntimeError` while the test body otherwise returns normally under repository pytest configuration
- **THEN** pytest exits nonzero and reports both the unique cause and `PytestUnhandledThreadExceptionWarning`
- **AND** the same config with only the exact filter removed exits zero with a passing test plus that warning, proving the shipping policy is load-bearing

#### Scenario: Unrelated warnings and baseline failures retain their behavior

- **WHEN** a test emits an unrelated `UserWarning` under repository configuration
- **THEN** the test remains passing with a warning rather than failing
- **AND** repository configuration adds no broad all-warning error filter or custom thread-exception hook
- **AND** final-head tracked tests and `conftest.py` files contain no explicit warning-filter override of this policy
- **AND** ordinary pytest markers or executed Python warning-filter calls are documented as intentional, reviewable overrides rather than claimed to be statically impossible
- **AND** a pre-existing non-thread-policy test failure is reported against its owning issue rather than suppressed, fixed out of scope, or counted as thread-warning debt

#### Scenario: Existing harness contracts remain stronger

- **WHEN** an issue-owned harness captures/re-raises a worker cause, releases peers, joins workers, cleans resources, or proves process termination
- **THEN** those local assertions remain required and unchanged
- **AND** a globally escalated warning cannot be used to waive a missing release, ordering, cleanup, or termination leg
