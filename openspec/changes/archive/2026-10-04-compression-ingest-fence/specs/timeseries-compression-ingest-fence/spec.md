## ADDED Requirements

### Requirement: Compression and ingest are ordered by a DB advisory fence
Every production ingest write transaction on a hypertable that the periodic compression compresses SHALL, as its first statement, try to take a shared transaction-scoped advisory fence for that hypertable's canonical family, without blocking. A canonical hypertable and its `_legacy` sibling share one fence, because they reference the same FK-referenced tables. Before each chunk compression, and before each retention `drop_chunks`, the lifecycle runner SHALL take the exclusive session-scoped fence for the same family while holding no other lock, with a bounded wait. For compression the wait is charged against the chunk's compress timeout; for retention it is bounded by the existing retention lock timeout.

#### Scenario: Ingest write starts while compression waits for or holds the fence
- **WHEN** an ingest writer tries the fence while compression is queued for it or holds it
- **THEN** the writer rolls back with a non-failing busy outcome, the run is not marked failed, and the next ingest tick retries it

#### Scenario: Ingest write in flight as compression reaches a chunk
- **WHEN** an ingest write transaction holds the shared fence as compression requests the exclusive fence
- **THEN** compression waits for the fence before starting any copy, and neither side fails with `40P01`

#### Scenario: Fence wait exceeds the bound
- **WHEN** compression cannot take the fence within its configured wait
- **THEN** the chunk is recorded as `deferred_contended` without being copied

#### Scenario: Retention drop on a compressed hypertable
- **WHEN** the periodic retention drops a chunk of a compressed hypertable with `drop_chunks`
- **THEN** it first takes the same exclusive fence in the same session, holding no other lock, bounded by its configured lock timeout, so an in-flight ingest write transaction finishes or defers and neither side fails with `40P01`
- **AND** when the fence is not acquired within that bound, nothing is dropped and the chunk is reported through the existing lock-contention refusal `RETENTION_DROP_FAILED:<schema>.<chunk>: lock-contention(55P03): ...`

### Requirement: Contended deferral is distinguishable from failure
The compression receipt and exit code SHALL distinguish chunks deferred because of fence contention from chunks that failed.

#### Scenario: Only deferrals
- **WHEN** every selected chunk either compressed or was deferred because of contention, and at least one was deferred
- **THEN** the outcome is `deferred`, the run exits 0, and the receipt lists the deferred chunks and their count

#### Scenario: A real failure
- **WHEN** a chunk compression fails for a reason other than fence contention
- **THEN** the outcome is `partial` and the run exits non-zero, as before

### Requirement: Every compressed-hypertable writer is fenced
A structural test SHALL fail when a function that writes to a compressed hypertable does not take the ingest fence before its other database statements in that transaction.

#### Scenario: New unfenced write site
- **WHEN** a change adds a DELETE or INSERT against a compressed hypertable without the ingest fence first
- **THEN** the wire-site invariant test fails and names the function
