## ADDED Requirements

### Requirement: Every scheduler file-provider writer participates in one fail-safe transaction

The system SHALL refresh every node-22 provider before its 168-hour consumer
limit and SHALL serialize canonical destination replacement on the
destination-derived lock across refresh, manual, readiness, and state
writers, with an expected-preimage check populated only by the refresh
runner's own lanes (registry, worker mirror, readiness, state, and rollback)
and the state-index copyback. The manual publisher CLI takes that
lock but supplies no expected preimage; its concurrency with the refresh timer
is operator-gated (runbook prohibition plus a CLI startup warning), not
CAS-gated.

#### Scenario: Valid full inventory is published

- **WHEN** the authoritative Basins source validates as the exact current live
  inventory (19 models on 2026-07-15 after removing duplicate
  `HHe-MAIN-02`) and the destination-derived lock is
  acquired
- **THEN** `publish_all_basin_scheduler_registry` validates packages and invokes
  `publish_scheduler_registry_manifest`
- **AND** readers observe a complete manifest-last payload bound to schema,
  checksum, generation time, model identities, and object evidence
- **AND** immutable registry packages exist only under the private
  `OBJECT_STORE_ROOT`, while the shared provider root contains only the three
  canonical provider files and their lock files
- **AND** the unchanged scheduler consumer resolves every registry package
  reference against the private root and fails closed if a referenced private
  package disappears
- **AND** a bounded terminal receipt is bound to those exact bytes.

#### Scenario: Immutable package versions follow complete source content

- **WHEN** the same publishable Basins model is discovered under another host
  root or bounded repair-run workspace with byte-identical content
- **THEN** its package version is unchanged and contains no absolute path or
  object-URI identity
- **AND** changing any required file, optional SHUD runtime file, CALIB file, or
  forcing CSV changes the package version before publication
- **AND** the publisher recomputes the planned identity and fails before object
  writes if the source changes between version planning and publication
- **AND** a repaired kashigeer-style model never reuses an existing immutable
  base version when the actual package or forcing content differs.

#### Scenario: Timer and manual writers contend

- **WHEN** any two canonical registry writers hold overlapping
  destination-derived lock windows
- **THEN** exactly one owns the shared destination-derived lock
- **AND** the contender replaces no canonical bytes and emits no success
- **AND** the existing manual CLI arguments and successful output remain
  compatible when no contention exists
- **AND** this lock guarantee does not extend to a manual publisher CLI run
  whose lock window does not overlap but whose snapshot precedes another
  writer's commit: the CLI supplies no expected preimage, so that window is
  covered by the operator prohibition, not by code.

#### Scenario: Readiness or state changes during renewal

- **WHEN** an authoritative readiness/state writer replaces a destination after
  refresh snapshots it but before renewal commits
- **THEN** the common destination write lock compares the current digest/inode
  to the refresh expected preimage before replace
- **AND** refresh exits `provider_preimage_changed` without overwriting the new
  authoritative entries
- **AND** writers acquire only one destination lock at a time, so the runner
  needs no multi-lock ordering and cannot deadlock across three providers.

#### Scenario: Concurrent authoritative generation is never rollback-owned

- **WHEN** any worker-mirror, shared-registry, readiness, or state publisher
  reports a typed expected-preimage conflict after an authoritative writer
  installs a newer generation
- **THEN** that lane supplies no transaction commit token and is excluded from
  rollback
- **AND** every earlier lane whose atomic postimage token still matches is
  restored in reverse order
- **AND** the concurrent bytes remain unchanged and the receipt reports
  `failed/provider_preimage_changed`
- **AND** an unknown write-after exception without a matching atomic postimage
  token reports `replace_uncertain` instead of guessing rollback ownership.

#### Scenario: Pre-commit failure preserves canonical identity

- **WHEN** configuration, discovery, package/reference validation, staging, or
  pre-commit verification fails before atomic canonical replacement
- **THEN** the previous manifest bytes, digest, inode, mode, owner/group, mtime,
  and consumer-visible identity remain unchanged
- **AND** immutable content-addressed packages already published by the existing
  publisher are bounded orphan candidates recorded in evidence and are never
  automatically deleted.

#### Scenario: Replace and post-commit failures are phase explicit

- **WHEN** replace/fsync, post-commit re-read, rollback, or receipt publication
  fails after canonical replacement may have occurred
- **THEN** the outcome is exactly `replace_uncertain`, `restored_previous`, or
  `published_receipt_failed`, never a false pre-commit preservation claim
- **AND** a certain invalid replacement may restore captured validated previous
  bytes atomically without claiming the old inode/mtime survived
- **AND** an uncertain or receipt-only failure exits non-zero and leaves the
  unchanged consumer to validate complete old-or-new bytes.

#### Scenario: Concurrent readers and temp cleanup remain safe

- **WHEN** discovery overlaps temp write/fsync/replace/rollback
- **THEN** readers see complete old or complete new JSON, never a partial file
- **AND** current-run temp files are removed only with certain identity;
  uncertain cleanup is capped safe root-relative residue.

## REMOVED Requirements

### Requirement: Every file-provider writer participates in one fail-safe transaction

**Reason**: It listed a "lifecycle" writer on the destination-derived lock. No model-lifecycle code path takes that lock
or writes the scheduler file plane: the post-commit publisher seams were never registered, and #3204 retires them. The
requirement is restated without that writer under a new name, and its scenario is renamed accordingly.

**Migration**: See the ADDED requirement "Every scheduler file-provider writer participates in one fail-safe
transaction". The lock participants are unchanged in code.
