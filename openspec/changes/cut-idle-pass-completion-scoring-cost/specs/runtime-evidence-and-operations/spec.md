## ADDED Requirements

### Requirement: Scheduler pass evidence SHALL summarise retention skips by reason

The retention block of scheduler pass evidence SHALL NOT list one entry per skipped key. It SHALL carry
`skipped_by_reason`, a mapping from each skip reason that occurred to its count, whose values sum to `counts.skipped`;
`skipped_sample`, at most 50 skipped entries whose reason is not `within_retention_window`, in the order retention
produced them; and `skipped_sample_omitted`, the number of such entries that did not fit. The `counts`, `frontier`,
`extra_roots`, `copyback_lock_failures`, `planned`, `deleted` and `failed` fields are unchanged, and what retention
plans, deletes, skips or fails is unchanged.

#### Scenario: A normal pass records counts instead of the key inventory

- **WHEN** retention skips many keys because they are within the retention window and one because its cycle name is unparseable
- **THEN** pass evidence has no `retention.skipped` list
- **AND** `retention.skipped_by_reason` holds both reasons with their counts, summing to `retention.counts.skipped`
- **AND** `retention.skipped_sample` holds the unparseable entry and no `within_retention_window` entry

#### Scenario: The sample is bounded

- **WHEN** more than 50 keys are skipped for reasons other than `within_retention_window`
- **THEN** `retention.skipped_sample` holds the first 50 and `retention.skipped_sample_omitted` holds the remainder count

#### Scenario: Over-limit compaction keeps the per-reason counts

- **WHEN** a pass payload is compacted because it exceeds `max_evidence_bytes`
- **THEN** the compacted retention block still carries `skipped_by_reason`
