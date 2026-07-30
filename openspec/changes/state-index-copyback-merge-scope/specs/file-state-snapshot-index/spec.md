# file-state-snapshot-index — delta for state-index-copyback-merge-scope

## ADDED Requirements

### Requirement: Copyback merge SHALL scope destination-side object verification and checkpoint copying to the winning merged source entries

The state-snapshot-index copyback merge SHALL keep today's source-side validation unchanged: the full source index (before `authoritative_run_ids` filtering) is validated with object verification against the private reference root, failing closed on any missing or checksum-divergent source object. Destination-index reading SHALL retain structural validation (unreadable or non-object payloads fail closed; schema, payload checksum, entry limits, required fields, URI safety, and identity/state-id uniqueness checks preserved) but SHALL NOT require destination-side object existence for pre-existing entries. Checkpoint copying SHALL iterate only the source entries that won the merge for their identity key — a source entry that loses the merge collision to a later destination entry SHALL NOT have its object copied, and a pre-existing destination entry whose object has been archived from the shared root SHALL be carried through the merge unchanged with its object NOT re-copied (no resurrection against the archive contract). The published index SHALL remain the full merged entry set (pre-existing destination entries plus winning source entries) — scoping applies to verification and copying only, never to the published set. The merge-internal index publish SHALL NOT re-run full-index object verification; integrity of newly published entries is guaranteed by the per-entry source checksum verification and post-write read-back comparison, which SHALL remain unchanged. Merge collision semantics, locking, and compare-and-swap preimage semantics SHALL remain byte-identical. Other callers of the index publish function SHALL keep their existing verification behavior, and the publish function's defaults SHALL NOT change.

#### Scenario: Archived destination objects no longer block new entries

- **WHEN** the destination index contains historical entries whose objects have been archived from the shared root and a copyback merges new authoritative source entries whose objects verify against the private reference root
- **THEN** the merge succeeds, the new entries and their objects are published, and the historical entries are preserved unchanged in the published index

#### Scenario: Archived objects are not resurrected

- **WHEN** a copyback merge completes against a destination index holding entries whose shared-root objects were archived
- **THEN** those objects are not re-copied to the shared root by the merge

#### Scenario: Losing source entries do not overwrite shared objects

- **WHEN** a merged source entry loses its identity-key collision to a destination entry with a later created_at
- **THEN** the destination entry is published for that key and the losing source entry's object is not copied to the shared root

#### Scenario: The published set is never narrowed

- **WHEN** a copyback merge publishes the destination index
- **THEN** the published entry count equals the pre-existing destination entries plus the net-new winning source entries

#### Scenario: Source-side integrity is not weakened

- **WHEN** a source entry has a missing or checksum-divergent object under the private reference root
- **THEN** the merge fails closed exactly as today

#### Scenario: Corrupt destination index still fails closed

- **WHEN** the destination index is unreadable or not a JSON object
- **THEN** the merge fails closed exactly as today

### Requirement: A receipted idempotent copyback replay SHALL exist for failed state-index copybacks

An operator-invoked replay tool SHALL re-run the state-index copyback for an explicit run-id set or for the runs of one or more explicit cycles, resolved from the source index by matching each entry's flat optional `cycle_id` field after normalizing the requested cycle identifier to the production lowercase-source form. It SHALL expose exactly two object-store roots (private reference and shared destination, defaulting to the production environment variables) with the index paths derived from them, and SHALL refuse equal or overlapping roots. It SHALL default to dry-run — a read-only preview that does not invoke the merge, changes no index content, and copies no objects — and require an explicit enforce flag to invoke the real merge code path used by production copyback. An empty run-id resolution SHALL exit non-zero with a structured reason and SHALL NOT invoke the merge. Enforce runs SHALL be idempotent (a repeated enforce run publishes no new entries and reuses all checkpoints) and SHALL write a JSON receipt (schema-versioned, recording mode, resolved run ids, entry counts before and after, and per-checkpoint outcomes) under the receipt root named by its environment variable. The tool SHALL NOT touch the orchestration journal, the registry, or canonical-readiness providers.

#### Scenario: Backlogged entries are recovered idempotently

- **WHEN** the replay tool is enforced for a cycle whose earlier copyback failed closed
- **THEN** the missing entries enter the destination index with their objects copied, a receipt records the before/after counts, and a second enforce run reports zero new entries with all checkpoints reused

#### Scenario: Dry-run changes nothing

- **WHEN** the replay tool runs without the enforce flag
- **THEN** no index content change and no object copy occurs, and the receipt/preview reports the resolved run ids and would-be new entry count

#### Scenario: Empty resolution fails closed

- **WHEN** the requested cycles or run ids resolve to no source-index entries
- **THEN** the tool exits non-zero with a structured reason and the destination index is not written
