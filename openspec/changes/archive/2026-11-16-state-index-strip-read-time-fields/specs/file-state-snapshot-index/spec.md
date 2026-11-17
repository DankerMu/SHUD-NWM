## ADDED Requirements

### Requirement: Every state-index publish SHALL NOT persist read-time injected fields

Every state snapshot index publish SHALL NOT persist the `index_generated_at` and `object_evidence` fields that
validation injects in memory on read. This covers both lanes, private and shared, and every publish path: snapshot
upsert, usable-flag update, scheduler file-provider refresh renewal, copyback, repair and retention prune. Readers
that compare a persisted index against a planned one (copyback merge, repair read-back) SHALL keep stripping these
fields on read, because legacy files may still carry them. Reads SHALL keep
injecting both fields in memory, so read-side consumers are unchanged. An index that already persists either field
SHALL still validate, and SHALL be written without them on its next publish.

#### Scenario: A usable-flag update does not re-persist read-time fields

- **WHEN** an operator or the scheduler sets the usable flag on an entry of an index that was loaded with the fields injected
- **THEN** the published index entries carry neither `index_generated_at` nor `object_evidence`
- **AND** for entries that carry every normalized key, the index's json node count equals that of the same entries published by copyback

#### Scenario: A legacy index carrying the fields is cleaned on its next publish

- **WHEN** a persisted index whose entries carry `index_generated_at` and `object_evidence` is read and then republished by any publish path
- **THEN** the read succeeds with both fields present in memory
- **AND** the republished entries no longer persist either field
