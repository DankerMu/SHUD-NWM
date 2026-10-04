## ADDED Requirements

### Requirement: In-flight-held passes do not clear the stall streak

The node-22 stall probe SHALL classify a pass with `submitted_count == 0`, `blocked_candidate_count == 0` and at least one skipped candidate whose reason is outside the scheduler's terminal skip-reason set as in-flight-held. Such a pass SHALL NOT count as progress and SHALL NOT clear the streak. When every non-neutral pass since the last progress pass is blocked or in-flight-held and that span is at least `NHMS_SCHEDULER_STALL_IN_FLIGHT_MINUTES`, the verdict SHALL be non-`ok`. The probe's terminal skip-reason set SHALL be pinned equal to the scheduler's by an automated parity test. An unknown reason, or a non-zero skipped count with a missing or malformed skipped list, SHALL be treated as in-flight. When the last progress pass lies outside the scan window the span SHALL be measured from the oldest scanned pass of the run and reported as a lower bound. The receipt SHALL carry the in-flight-held count.

#### Scenario: All candidates skipped as active duplicates for longer than the gate
- **WHEN** the newest passes all have zero submissions, zero blocked candidates and candidates skipped as `active_duplicate_pipeline`, and the last progress pass is older than the gate
- **THEN** the verdict is not `ok` and the receipt shows the in-flight-held count

#### Scenario: Healthy forecast in flight
- **WHEN** in-flight-held passes span less than the gate since the last progress pass
- **THEN** the verdict stays `ok`

### Requirement: Suppression does not apply to the current frontier

A no-progress tracker entry matching a suppressed reason SHALL remain suppressed only when its source and cycle are absent from the candidate and skipped cycles of the newest non-neutral pass. An entry on the frontier SHALL be graded as if unsuppressed and SHALL be marked `suppression_bypassed_frontier` in the receipt.

#### Scenario: Frontier entry with a suppressed reason
- **WHEN** a `comment_accounting_unproven` entry names a cycle present in the newest pass
- **THEN** it participates in the verdict and the receipt marks it `suppression_bypassed_frontier`

#### Scenario: Old-cycle entry with a suppressed reason
- **WHEN** the only matching entries name cycles absent from the newest pass
- **THEN** they stay in `suppressed[]` and the verdict is unaffected
