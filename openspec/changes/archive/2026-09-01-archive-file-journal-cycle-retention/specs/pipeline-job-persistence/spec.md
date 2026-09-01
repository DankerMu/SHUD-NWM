## ADDED Requirements

### Requirement: Expired file-journal cycles SHALL be archived only as complete, inactive, recoverable authority slices

The node-22 file-journal retention entrypoint SHALL derive age from a cycle's `%Y%m%d%H` identity and SHALL use a 90-day default hot-retention window. Its candidate set SHALL be the complete union discovered from exactly the hot `latest/`, `journal/`, and `pipeline-events/` roots under one aggregate existing journal file-count bound and the existing journal depth bound. A failed root walk or exhausted bound SHALL block the entire enforce invocation rather than return a partial candidate set. For one normalized `(source_id, cycle_time)` slice it SHALL consider only that cycle's recognized `latest` views, bounded `journal` segments, and bounded `pipeline-events` segments. It SHALL NOT scan for candidates in, remove, or rewrite `pipeline-jobs`, reconcile inventory, cycle lock files, archives, or the scheduler state index.

Before enforcement the entrypoint SHALL validate that the configured retention window exceeds scheduler lookback plus cycle lag plus twice the largest allowed-cycle gap. It SHALL require a fresh pipeline frontier and SHALL exempt every cycle at or after a non-null active lower bound. Missing, stale, malformed, or retention-disabled frontier evidence, and invalid or missing safety-window configuration, SHALL prevent all hot-file removal rather than degrade to wall-clock deletion.

For each candidate the entrypoint SHALL acquire the same cross-process cycle lock used by journal writers without waiting indefinitely, inspect the canonical cycle replay, and apply the journal owner's existing rollback/reconcile and released-identity-blocked predicates. Any live row, lock contention, malformed or unrecognized member, symlink or non-regular member, source/cycle mismatch, segment gap/overflow, disappearing file, or unreadable input SHALL leave the entire cycle untouched with a stable reason in the receipt. Retention SHALL NOT rely on reconcile inventory alone because a released identity-blocked row is intentionally absent from that inventory.

An eligible cycle SHALL be written as one `tar.zst` archive plus a versioned manifest that binds the normalized source/cycle, frontier evidence, archive SHA-256, and every member's relative path, size, and SHA-256. The implementation SHALL write and verify a temporary archive, atomically publish it without clobbering a conflicting bundle, and only then unlink the exact recognized hot members while still holding the cycle lock. Dry-run SHALL be the default, enforcement SHALL require explicit enablement and `dry_run=false`, and every invocation SHALL emit a bounded receipt. A matching existing archive and a partially removed hot slice SHALL be safely retryable; a conflicting archive SHALL block the cycle.

Cold archives SHALL NOT become a transparent read tier. Recovery SHALL be an explicit, documented operation that verifies archive and member identities, rejects path escape and overwrite, restores the original relative paths while the selected cycle has no writers, and proves cycle-query parity before normal operation resumes. Future retention of the scheduler state index is outside this capability and MUST preserve at least one usable state-index history anchor at or before relevant candidate cutoffs for each existing `(model_id, source_id)`; this retention entrypoint SHALL leave the index byte-identical.

#### Scenario: Dry-run plans an eligible complete cycle without mutation

- **WHEN** a recognized `gfs` or `IFS` cycle is older than 90 days, precedes a fresh active frontier, contains only quiescent terminal rows, and retention is in its default dry-run mode
- **THEN** the receipt lists the complete `latest`, `journal`, and `pipeline-events` member plan and byte count
- **THEN** no archive or hot-authority member is created, removed, or rewritten

#### Scenario: Enforce archives before removing only the hot cycle slice

- **WHEN** the same eligible cycle is processed with retention explicitly enabled and dry-run disabled
- **THEN** one verified `tar.zst` bundle and manifest are atomically published before any hot member is unlinked
- **THEN** only that cycle's recognized hot members are removed, while `pipeline-jobs`, reconcile inventory, locks, and state index remain byte-identical

#### Scenario: A recoverable row retains the whole cycle

- **WHEN** canonical replay contains an unbound reservation, a current accepted master with incomplete terminal projections, a rollback-blocking row, or a released identity-blocked reservation even though reconcile inventory has no anchor for it
- **THEN** retention records `live_row` for the candidate and leaves every member of the cycle untouched

#### Scenario: Candidate discovery is complete or enforcement stops

- **WHEN** all three hot roots contain a large-but-valid candidate set at the aggregate file/depth bounds
- **THEN** every normalized candidate is discovered exactly once regardless of which hot surface introduced it
- **WHEN** one more entry exceeds the aggregate bound or any root walk fails
- **THEN** enforcement archives and removes nothing rather than operating on a truncated candidate set

#### Scenario: Active or unprovable scheduler windows fail closed

- **WHEN** the candidate is at or after the fresh pipeline frontier, the configured hot window violates the lookback/lag/cycle-gap inequality, or frontier evidence is missing, stale, malformed, or from a pass whose retention did not run
- **THEN** enforcement removes no hot member and records the precise exemption or blocker
- **THEN** there is no override that falls back to pure wall-clock deletion

#### Scenario: Concurrent or malformed cycle authority is not partially archived

- **WHEN** the cycle lock is busy or any recognized slot is symlinked, non-regular, unreadable, gapped, over the segment cap, mismatched, replaced during inspection, or accompanied by an unrecognized cycle-shaped member
- **THEN** retention skips or blocks the entire cycle without waiting indefinitely
- **THEN** no archive is published and no hot member is removed

#### Scenario: Archive publication and cleanup are idempotent

- **WHEN** a prior attempt published a byte-identical verified archive but removed only a subset of its hot members
- **THEN** a retry verifies the existing archive and removes only remaining manifest-bound members
- **THEN** a pre-existing archive with a different manifest or digest blocks the cycle without overwriting either copy

#### Scenario: Restore reproduces the archived cycle without clobbering authority

- **WHEN** an operator verifies a cold bundle, stages every member under containment, and restores it while the selected cycle has no writers
- **THEN** path traversal, symlinks, unexpected members, digest mismatches, and existing destination files fail closed
- **THEN** a successful restore reproduces every member digest and the captured cycle-scoped query result

#### Scenario: State history semantics are unchanged

- **WHEN** retention dry-run, enforce, or restore is executed
- **THEN** the scheduler state-index file and its usable entries remain byte-identical
- **THEN** an existing model cannot be reclassified as `cold_new_model` because journal retention removed a state-index history anchor
