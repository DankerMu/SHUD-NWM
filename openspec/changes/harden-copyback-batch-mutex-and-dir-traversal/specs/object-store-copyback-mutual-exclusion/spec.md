## ADDED Requirements

### Requirement: Copyback directory-tree promote-and-commit batches are mutually exclusive across processes

A process SHALL hold one exclusive cross-process mutex, fixed per copyback root,
for the whole of any critical section in which it promotes a directory tree under
the shared object-store copyback root — from planning, through every
directory-tree promotion, until its batch commit or batch rollback has returned. The mutex SHALL NOT be released between the individual tree
promotions of one batch.

A writer that cannot acquire the mutex within its configured deadline SHALL fail
loudly with a distinct error and SHALL NOT promote any tree.

#### Scenario: a competing writer cannot enter the backup-to-promote window

- **WHEN** one writer is between renaming an existing copyback target to its
  backup name and promoting its own temporary tree into that name
- **AND** a second copyback writer starts against the same copyback root
- **THEN** the second writer MUST block until the first writer's batch has
  committed or rolled back
- **AND** neither writer's reported status MAY claim success for a tree that is
  no longer present at its target.

#### Scenario: a batch rollback never removes another writer's committed tree

- **WHEN** a writer promotes a tree into a target that did not previously exist,
  so its rollback entry records no backup directory
- **AND** the same writer later fails on a subsequent tree in the same batch
- **THEN** the rollback MUST NOT remove a tree that a different writer committed
  after that promotion
- **AND** the destination MUST hold exactly one writer's complete tree.

#### Scenario: the one-off backfill writers take the same mutex

- **WHEN** `scripts/canonical_precip_copyback_backfill.py` or the forcing
  copyback backfill writes under the same copyback root as the publisher
- **THEN** it MUST hold the same mutex for its own critical section
- **AND** no file it reports as copied MAY be removed by a concurrent publisher's
  rollback
- **AND** a backfill MUST acquire the mutex per mirrored tree rather than once
  for its whole run, so a long backfill cannot starve a publisher past its
  deadline; per-tree is sufficient because a backfill mirrors file by file with
  no cross-tree rollback of its own
- **AND** a backfill mode that provably writes nothing — `--dry-run` — MUST NOT
  acquire, because creating the lock file is itself a write under the copyback
  root.

#### Scenario: the run-tree copyback lane is covered by the same adjudication

- **WHEN** `services/orchestrator/run_tree_copyback` replaces a run tree under
  the shared copyback root
- **THEN** it MUST hold the same mutex
- **AND** its guarded recovery branch — which restores its backup only when the
  target is absent, and therefore never removed a competitor's tree — MUST remain
  documented in place as a benign spurious-failure terminal state rather than a
  lost update
- **AND** the state-snapshot index merge it performs in the same call MUST run
  after the mutex has been released, per the per-file exemption below; its
  presence in the call's returned summary is reporting, not a promote held open
  across the release.

#### Scenario: per-file provider-atomic copyback writes are exempt

- **WHEN** a copyback **write** promotes no directory tree — it writes an
  individual file under a disjoint subtree of the copyback root through its own
  provider-atomic lock, as the state-snapshot index merge and the state
  checkpoint copyback do
- **THEN** that write is outside this requirement and MUST proceed without
  holding this mutex
- **AND** when the same call also promotes directory trees, the exempt write
  MUST run only after this mutex has been released — never nested inside it,
  because the provider-atomic lock it takes has no deadline and nesting an
  unbounded wait inside the bounded mutex turns one stalled writer into a
  head-of-line stall for every other writer under the copyback root.

The exemption is scoped to the write, not to the writer: a caller that promotes
directory trees *and* performs such a write is covered by this requirement for
its promotes and exempt for that write.

#### Scenario: the mutex fails closed on an unsafe lock file

- **WHEN** the lock path is a symlink, is not a regular file, has more than one
  hard link, does not have mode `0o600`, is not owned by the effective user, or
  is not owned by the same uid that owns the copyback root
- **THEN** acquisition MUST raise rather than proceed, as each lane's own error
  type and distinctly from a timeout — the q_down publish lane raises a publish
  error carrying a copyback-lock-unsafe code, so an operator can tell a tampered
  lock file from a busy one
- **AND** no copyback tree MAY be promoted
- **WHEN** the lock file does not exist yet and the effective user is not the
  copyback root's owner
- **THEN** acquisition MUST refuse *before* creating it, because the lock file is
  never unlinked — creating it would leave exactly the foreign-uid orphan that
  poisons the lock for every legitimate writer
- **AND** no lock file MAY be left behind by the refused writer.

#### Scenario: the mutex is anchored where every writer provably shares it

- **WHEN** two copyback writers run under different mount namespaces — a private
  `/tmp` from a service sandbox or a job container, for example
- **THEN** they MUST still contend on the same lock, because the lock is anchored
  under the copyback root each of them already resolved
- **AND** two distinct copyback roots MUST NOT contend with each other.

#### Scenario: acquisition deadline is bounded and observable

- **WHEN** the mutex is held by another writer for longer than the configured
  timeout
- **THEN** the waiting writer MUST raise a distinct timeout error
- **AND** it MUST NOT fall back to an unsynchronized promote
- **AND** where the caller already records a failure receipt instead of
  propagating — as the `convert`-stage canonical precipitation mirror does — the
  surrounding cycle MUST survive with that receipt.

#### Scenario: the timeout surfaces as each lane's own error type

- **WHEN** the mutex acquisition times out
- **THEN** the raised error MUST be of the type each calling lane already handles,
  so no lane gains an uncaught exception: the run-tree copyback lane MUST raise its
  own run-tree copyback error, and the q_down publish lane MUST raise a publish
  error carrying a distinct copyback-lock-timeout code
- **AND** no lane MAY propagate a foreign exception type past a stage handler that
  catches only its own.

#### Scenario: a zero-write skip path creates no lock file

- **WHEN** the configured copyback root resolves to the same directory as the
  object-store root, so the lane returns a skip without writing anything
- **THEN** the mutex MUST NOT be acquired and no lock file MAY be created
- **AND** acquisition MUST therefore happen after the copyback root's identity and
  overlap guards, not merely after the root has been prepared.
