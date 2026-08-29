## ADDED Requirements

### Requirement: State snapshot index repair SHALL be exact, archive-first, and production-validated

The system SHALL provide an operator repair entrypoint with `remove-entry` and `recompute-checksum` operations over the production two-lane topology: a private/reference root (default `OBJECT_STORE_ROOT`) and shared/destination root (default `NHMS_OBJECT_STORE_COPYBACK_ROOT`), with each index path fixed at `scheduler/state-index/index-last.json`. It SHALL refuse roots that are equal, overlap, or alias the same filesystem identity. It SHALL default to a read-only dry-run and SHALL require an explicit enforce flag for mutation. Both operations SHALL read bounded stable provider snapshots and apply every production state-index structural, schema, entry, URI-safety, uniqueness, size, and complexity rule; the helper MAY rebuild the expected top-level checksum solely to permit validation of an otherwise intact checksum-mismatched payload, but SHALL NOT accept malformed JSON or any other invalid contract. Historical state-object existence and freshness SHALL NOT be required because repair does not change state object references and historical shared objects may be archived.

The two indexes SHALL NOT be treated as whole-file mirrors or copied over one another; each lane SHALL preserve its own unrelated entry mappings and order. `remove-entry` SHALL accept exactly one selector family — exact `state_id`, exact `run_id`, or the complete `(model_id, source_id, valid_time)` tuple — resolve it independently in both lanes, require exactly one matching logical identity in both by default, and require matching production identity key plus `state_id` across the two matches. An explicit missing-lane flag MAY authorize a dry-run-proven one-lane absence, but zero/multiple/different matches without that exact disposition SHALL refuse before either index write. Enforce SHALL acquire reference then destination locks in the same order as production copyback, re-read and bind both pre-images under lock, archive both exact pre-images before the first CAS, remove the reference match first, then remove the destination match. Each replacement SHALL use the production checksum/canonical publication path and production read-back validator. This source-first order SHALL ensure that a destination failure cannot leave a stale reference entry able to re-enter shared through copyback.

`recompute-checksum` SHALL require an explicit `reference` or `destination` lane, mutate only that lane, preserve its raw `entries` list semantically, validate the other topology lane read-only, and record why the other lane was untouched. It SHALL archive the target lane's exact pre-image before its CAS.

The entrypoint SHALL write a bounded schema-versioned per-lane receipt after all reachable enforce read-backs. It SHALL distinguish success, provably zero-index-write refusal, and partial/committed/commit-uncertain incomplete outcomes with stable statuses and exit codes. Archive failure before the first CAS and provider errors proven to precede the first CAS SHALL be refusals. Once either lane may have been replaced, any later lane failure, lock release, post-write read-back, or receipt publication failure SHALL never claim both indexes were unchanged. Dry-run SHALL create no locks, archive/receipt directories, archive/receipt files, or index writes.

#### Scenario: Checksum-only repair preserves the selected lane and leaves its sibling untouched

- **WHEN** an otherwise production-valid destination payload has a missing or mismatched top-level checksum and an operator first previews and then enforces `recompute-checksum --lane destination` with valid private archive and receipt roots
- **THEN** dry-run changes no filesystem bytes, enforce archives the exact destination pre-image before CAS, the committed destination passes the production validator with a semantically identical and identically ordered raw `entries` list, the reference bytes are unchanged, and the receipt records the reference lane as validated but intentionally untouched

#### Scenario: One exact logical entry is removed from both lanes

- **WHEN** `remove-entry` is enforced with one selector family that resolves uniquely in reference and destination to the same production identity key and `state_id`
- **THEN** both exact pre-images are archived before the first write, the entry is removed from reference before destination, it is absent from both production-valid read-backs, each lane preserves every unrelated raw entry mapping and order, and the receipt binds both operations to per-lane pre-image and post-image digests

#### Scenario: Ambiguous, absent, or cross-lane-divergent selector refuses without index mutation

- **WHEN** a removal selector resolves to zero entries or more than one entry in a required lane, including a non-unique `(model_id, source_id, valid_time)` base tuple, or the two lane matches disagree on production identity or `state_id`
- **THEN** the entrypoint returns a stable zero-index-write refusal and writes no archive, index replacement, or receipt

#### Scenario: Explicit one-lane absence is recorded

- **WHEN** dry-run proves the selected logical entry is absent from exactly one lane and the operator repeats enforce with the corresponding explicit missing-lane flag
- **THEN** the present lane alone is archived and repaired, the absent lane stays byte-identical, and the receipt records the exceptional topology disposition instead of silently treating absence as success

#### Scenario: Structural corruption is not blessed by checksum repair

- **WHEN** the payload is malformed JSON, has an unsupported schema, invalid or unsafe entries, duplicate identity/state IDs, or exceeds byte/entry/JSON complexity limits
- **THEN** both operations refuse before archive or index mutation rather than merely recalculating a checksum

#### Scenario: Archive failure before the first CAS is a refusal

- **WHEN** enforce cannot durably write and read back every required exact pre-image archive before the first lane CAS
- **THEN** both indexes remain unchanged and the entrypoint reports a zero-index-write refusal

#### Scenario: Destination failure after reference removal is partial, not refusal

- **WHEN** reference removal commits and the destination CAS or read-back then fails
- **THEN** the entrypoint reports a partial or commit-uncertain incomplete outcome with a non-refusal exit code, records each lane's actual/uncertain state, and never repopulates reference from destination; the stale destination remains conservative while reference can no longer resurrect the entry through copyback

#### Scenario: Post-replace uncertainty never claims refusal

- **WHEN** either lane replacement may have occurred and a later lock release, provider verification, production read-back, or receipt write fails
- **THEN** the entrypoint reports a partial/committed/commit-uncertain incomplete outcome with a non-refusal exit code and emits the known bounded per-lane repair summary for operator reconciliation

#### Scenario: Repair and copyback share one lock order

- **WHEN** a repair and production copyback contend for the same reference and destination indexes
- **THEN** both acquire reference before destination, no lock-order deadlock is introduced, and per-lane pre-image checks prevent stale publication

#### Scenario: Normal publishers and copyback remain compatible

- **WHEN** existing state-index publish, refresh, strict lookup, or copyback replay paths run without invoking the repair entrypoint
- **THEN** their validation defaults, object checks, locking, CAS, payload format, and behavior remain unchanged
