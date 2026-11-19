## ADDED Requirements

### Requirement: Over-limit compaction SHALL keep the retention skip summary

When scheduler pass evidence is compacted because it exceeds `max_evidence_bytes`, the compacted retention block SHALL
keep `skipped_summary` and `skipped_omits`, so the per-root, per-reason skip counts survive the removal of the
per-entry retention lists.

#### Scenario: A compacted pass still tells how many keys were skipped and why

- **WHEN** a pass payload exceeds `max_evidence_bytes` and its retention entry lists are replaced by their summary
- **THEN** the compacted retention block carries `skipped_summary` with the same rows as the uncompacted block
- **AND** it carries `skipped_omits`
