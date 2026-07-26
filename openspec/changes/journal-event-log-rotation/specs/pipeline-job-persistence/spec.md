# pipeline-job-persistence — delta (#1165)

## ADDED Requirements

### Requirement: Per-cycle journal event logs SHALL rotate into bounded segments and journal capacity faults SHALL NOT fail-close unrelated scheduler work

When appending a record (or record batch) to a per-cycle journal event log would exceed the configured per-file byte limit, the journal SHALL roll the write over to a new continuation segment of the same cycle instead of failing, keeping every segment within the limit; readers SHALL replay all segments of a cycle in segment order with a globally monotonic replay order equivalent to a single concatenated log. A record or batch that exceeds the byte limit by itself SHALL still fail exactly as before. During restart reconciliation, a journal capacity or integrity error raised while resolving one reserved-unbound row SHALL quarantine that row with recorded evidence (reason and offending file) and SHALL NOT abort resolution of the remaining rows or the scheduler pass.

#### Scenario: Append near the byte limit rolls over to a continuation segment

- **WHEN** a cycle's newest journal segment cannot fit the next event
  line within the per-file byte limit
- **THEN** the line is written to a new continuation segment of the same
  cycle, both segments remain within the limit, and a subsequent replay
  of the cycle yields the same rows and ordering as if all lines lived
  in one file

#### Scenario: Single-segment cycles read byte-identically to today

- **WHEN** a cycle's event log never overflowed
- **THEN** reads, replay order, and computed event ids are identical to
  the pre-rotation behavior

#### Scenario: Oversized single record still fails closed

- **WHEN** one record (or one batch) alone exceeds the per-file byte
  limit
- **THEN** the append fails with the existing byte-limit error and no
  partial content is written

#### Scenario: Reconcile quarantines a poisoned cycle instead of aborting the pass

- **WHEN** restart reconciliation hits a journal error while resolving
  one reserved-unbound row
- **THEN** that row is recorded as quarantined in the reconcile evidence
  with the error reason and offending file, the remaining reserved rows
  are still resolved, and the scheduler pass proceeds past restart
  reconcile

#### Scenario: Journal error evidence names the offending file

- **WHEN** a journal byte-limit or integrity error surfaces in
  restart-reconcile evidence
- **THEN** the evidence message includes the redacted offending file
  reference, not only the bare reason string

#### Scenario: Existing enumeration readers tolerate continuation segments

- **WHEN** a cycle has continuation segments and a journal-wide
  enumeration reader (pipeline-job queries by cycle/run/slurm-id,
  rollback-scope iteration, reconcile-inventory backfill, cycle source
  discovery) walks the journal surface
- **THEN** continuation segments resolve to their base cycle — no
  invalid-cycle-time error and no silently skipped segment records —
  replay and inventory backfill arbitrate records in segment order
  (never lexicographic path order), genuinely unparseable file names
  keep today's behavior, and an orphan (gapped) segment WITHIN the
  bounded probe window (indices up to the segment cap) fails closed
  with the same answer from every reader; an orphan BEYOND the probe
  window — unreachable by the writer, producible only by external
  corruption — still fails closed in the enumeration walkers and
  inventory backfill, while the cycle-level exact-path reader cannot
  observe it (no-globbing hot-path constraint), an asymmetry that is
  pinned by test and documented where operators diagnose it

#### Scenario: Latest-view precedence survives segmented replay order

- **WHEN** a latest-view row and a continuation-segment journal record
  tie on the same sequence
- **THEN** the latest view still wins, exactly as it does for
  single-segment cycles

#### Scenario: Segments per cycle are bounded

- **WHEN** a cycle has reached the configured maximum number of
  segments and another rollover would be required
- **THEN** the append fails closed with a distinct
  segment-limit-exceeded reason naming the cycle file, bounding a
  cycle's total journal capacity and keeping segment exhaustion
  distinguishable from an oversized single record
