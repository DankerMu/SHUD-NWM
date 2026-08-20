# pipeline-job-persistence Spec Delta

## ADDED Requirements

### Requirement: The journal's cache fast path SHALL be granted by cycle-write-window ownership, never by the mere fact that some thread holds the write lock

The cycle-rows cache serves a hit without revalidating its source files only inside a cycle write window, on the premise that the cycle flock excludes other writers and the in-window append hook keeps the cache coherent. Both halves of that premise hold **only for the thread that owns the window, and only for the cycle that window covers**: the flock protects that one cycle, and the append hook maintains coherence for the records that owner writes. The fast-path predicate SHALL therefore be true when and only when the calling thread is itself inside a write window for the very cycle being read, so that a thread which merely observes another thread's write in progress — or which reads a different cycle from inside a window — falls back to full source-file revalidation, exactly as it would in a single-threaded run. The predicate SHALL NOT be satisfied by holding a write lock taken for work that is not a cycle write: a lock held for reconcile-inventory maintenance takes no cycle flock and runs no append hook, so it establishes neither half of the premise and SHALL grant no fast path. Because the ownership marker is what makes the fast path safe, its lifetime SHALL be bounded by the same construct that opens the window and SHALL be cleared on every exit path including exceptions — including a failure raised while the window is being established, not only one raised from the work inside it — because a marker leaked past the window would hand a fast path to whatever unrelated task next reuses that thread identity. Only one construct SHALL set the marker, so that pairing is a structural property of that construct rather than a convention repeated across call sites. Entries cached during a window carry no fingerprint and are therefore unvalidatable, so the wipe performed when a window opens SHALL be treated as a correctness precondition of the fast path rather than as a performance measure: without it the owner would serve a pre-window entry that a writer in another process may have already invalidated. The fast path narrows the tamper exposure that the fingerprint would otherwise detect from any thread down to the window owner alone; the owner's own fast path still performs no tamper detection, which is a separate pre-existing concern this requirement does not address.

#### Scenario: A non-owner thread revalidates instead of trusting a hit

- **WHEN** one thread is inside a cycle write window and another thread, sharing the same repository instance, reads cycle rows for a different cohort whose cached entry is stale
- **THEN** the reading thread revalidates the source files and returns freshly recomputed rows, never the stale cached rows

#### Scenario: The owner keeps its fast path

- **WHEN** the thread that owns a cycle write window reads cycle rows inside that window
- **THEN** the cached rows are served without computing a source fingerprint at all

#### Scenario: A window for one cycle grants nothing for another

- **WHEN** the thread that owns a write window for one cycle reads cycle rows for a different cycle
- **THEN** the read revalidates its source files, because the window's flock protects only its own cycle

#### Scenario: A lock held for non-cycle work grants nothing

- **WHEN** a thread holds the repository write lock for work that takes no cycle flock and runs no append hook
- **THEN** cycle-rows reads on any thread, including that one, still revalidate their source files

#### Scenario: The marker does not survive an exception in the window's body

- **WHEN** the body of a cycle write window raises
- **THEN** the ownership marker is cleared before the exception propagates, so a later task reusing the same thread identity gets no fast path

#### Scenario: The marker does not survive a failure while the window is opening

- **WHEN** establishing the window fails before its body is ever entered
- **THEN** the ownership marker is cleared just the same, because the thread identity is released back to the pool either way

#### Scenario: A non-owner read does not depend on cache-clearing granularity

- **WHEN** the cycle write window's cache clearing is disabled entirely and a thread that owns no window reads a different cycle
- **THEN** that read still returns correct values, because a non-owner read rests on revalidation rather than on eviction

#### Scenario: An owner read does depend on the window-entry wipe

- **WHEN** the same clearing is disabled and the window owner reads its own cycle, for which a pre-window entry was cached and then invalidated by another process
- **THEN** the owner would serve that stale entry — which is why the window-entry wipe is a correctness precondition and not a tunable

### Requirement: A journal read SHALL absorb a concurrent atomic replacement of the file it is reading, within a bounded number of attempts

The journal's durable writes replace files atomically, which changes the target inode; the hardened reader rejects an inode change observed between its pre-open stat and its post-open fstat. Those are the same event at that layer, so a perfectly normal concurrent write is otherwise reported as a containment failure and then fans out into inconsistent outcomes — a silently skipped submission on one read path, a fabricated running status on another, a whole-source submission failure on a third. A read issued through the repository's cached read chokepoint — the single helper every journal document and event-log read passes through — SHALL therefore retry when it fails solely because the target was replaced mid-open, because an inode change means a writer just finished and re-reading yields the newly committed content. The guarantee is scoped to that chokepoint deliberately, and SHALL NOT be read as a promise about a journal read that bypasses it: the retry is inherited by routing through the chokepoint, not by being a journal read, and a future read path that opens the primitive directly carries no retry. The retry SHALL be bounded by a named constant and SHALL carry no sleep, and once the attempts are exhausted the read SHALL fail exactly as it does today rather than degrading to an empty or default result. The retry SHALL be selected on a structured discriminator carried by the error, never by matching its message text. Every other refusal the hardened reader can raise — symlinked target, non-regular target, containment violation, a symlink appearing inside the open window — SHALL NOT be retried even once, so the reader's fail-closed behavior is unchanged for every case except the one that was never an attack signal.

#### Scenario: A replacement landing inside the open window is absorbed

- **WHEN** the target file is atomically replaced between the reader's pre-open stat and its open
- **THEN** the read retries and returns the content committed by the replacement

#### Scenario: A relentless writer still fails closed

- **WHEN** every attempt observes a fresh replacement
- **THEN** the read makes exactly the bounded number of attempts and then raises, rather than retrying without limit or returning a default

#### Scenario: Safety refusals are never retried

- **WHEN** the read fails because the target is a symlink, is not a regular file, escapes the containment root, or becomes a symlink inside the open window
- **THEN** exactly one attempt is made and the refusal propagates unchanged

#### Scenario: Retry selection reads a field, not a message

- **WHEN** a read failure carries the same human-readable message but not the structured replacement discriminator
- **THEN** it is not retried

#### Scenario: Two threads read and write the same cycle

- **WHEN** two threads share one repository instance and concurrently read and write the same cycle
- **THEN** the reads complete without a containment failure, so an end-to-end test no longer has to separate reader and writer onto different cycles to stay green
