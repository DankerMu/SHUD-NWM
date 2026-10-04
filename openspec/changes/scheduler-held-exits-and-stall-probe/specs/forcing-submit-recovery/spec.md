## ADDED Requirements

### Requirement: Operator-verified absence exit for held forcing rows

The file journal SHALL provide an operator-verified absence exit for a forcing master that is `reserved`, unbound and `submit_result_ambiguous` with a complete forcing submit identity. The exit SHALL, under the journal lock, re-check stage, identity completeness, status, absence of a binding, the held state, and that the operator-supplied attempt and anchor equal the row's; any mismatch SHALL be rejected by name with zero writes. It SHALL also refuse a `checked_at` that is in the future or earlier than the attempt anchor plus the absence grace. On success it SHALL write the same retry-permitting row state as the automatic absence permit and record `checked_by`, `checked_at` and `verification_note` in an `operator_verified_absence` pipeline event, not on the row, and SHALL perform no sbatch and no scancel. `checked_by` and `verification_note` SHALL be operator-supplied and non-blank; there is no default.

#### Scenario: Held forcing row with no job in sacct
- **WHEN** the operator runs the exit with matching attempt and anchor, a verifier and verification evidence
- **THEN** the row becomes retryable, its members are no longer held-skipped, the audit fields are recorded and nothing is submitted

#### Scenario: Rejected exit
- **WHEN** the attempt or anchor does not match, the row is not held, is already bound, is not a forcing row, or the verifier or evidence is blank
- **THEN** the command fails naming the reason and the journal is byte-identical
