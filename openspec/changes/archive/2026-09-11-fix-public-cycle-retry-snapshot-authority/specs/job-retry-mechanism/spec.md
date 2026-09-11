## ADDED Requirements

### Requirement: Automatic public-cycle retry identity uses the selected stage snapshot

When an automatic public-cycle retry decision selects a predecessor from a jobs snapshot, the replacement retry identity SHALL be derived from that same snapshot. A later repository read SHALL NOT advance the replacement suffix independently of the selected predecessor's retry decision. Concurrent passes using the same snapshot and retry permission SHALL derive the same replacement identity so that the existing atomic reservation permits at most one gateway submission.

The explicit context retry-attempt precedence, existing stage/prefix/suffix handling and minimum attempt, retry eligibility predicates, operator-demoted recovery's reuse of its current key, and exact-key reservation/reclaim rules SHALL remain unchanged. The implementation SHALL NOT serialize gateway submission or weaken accepted-submit reconciliation evidence to achieve this property.

#### Scenario: Second worker calculates an ID after the first reservation commits

- **WHEN** two public forecast-cycle passes select the same absence-permitted attempt2 snapshot and the second computes its retry ID only after the first reserves attempt3
- **THEN** both derive the same replacement key, only one owns a reservation and enters the gateway, and all cohort projections report attempt3 without an attempt4 submission

#### Scenario: Repeated authoritative absence preserves submit-once across rounds

- **WHEN** an initial ambiguous forecast submission is followed by authoritative absence and two concurrent public passes, then a second absence decision and two more concurrent passes
- **THEN** cumulative forecast gateway calls are2 then3, each round has exactly one reserved-unbound master with clean reconciliation fields, all cohort hydro rows agree with its attempt, and no cancellation is introduced

#### Scenario: Explicit retry and sibling retry doors retain their semantics

- **WHEN** a valid explicit context retry attempt is present, or the caller retries a missing-raw download or a terminal stage after upstream refresh
- **THEN** explicit attempt precedence and existing retry eligibility/suffix rules are preserved, while automatic suffix computation uses the same selected jobs snapshot
- **AND** operator-demoted recovery continues reusing its old master key through the existing reclaim transition
