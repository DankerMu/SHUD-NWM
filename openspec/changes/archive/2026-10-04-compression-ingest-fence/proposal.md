# compression-ingest-fence

## Why
- **#2713, measured on node-27 on 2026-10-04.** The periodic timeseries compression lost two deadlocks to concurrent output-parser write transactions, both reported as `40P01 deadlock detected`, after 35 and 20 minutes of copy work. It made zero progress that tick and exited 1.
- **How the cycle forms.**
  - A parser write is a single transaction. Its probe takes AccessShare on **every** chunk, and its DELETE requests RowExclusive on every chunk, even though planning prunes the delete to 7 chunks.
  - `compress_chunk` holds a lock that conflicts with RowExclusive while it copies, then upgrades to AccessExclusive to finish. The upgrader is the one that detects the deadlock, so compression always loses.
- **Why the existing exclusion does not cover it.** The lifecycle flock (`node27_timeseries_lifecycle_lock`) deliberately leaves autopipe out, on the argument that it is correctness-safe. That argument is about correctness, not lock liveness.

## What Changes
- **A DB-level advisory read/write fence per canonical hypertable family (a canonical table plus its `_legacy` sibling).**
  - Every ingest write transaction on a compressed hypertable *tries*, as **the first statement of the write transaction**, to take a shared transaction-scoped fence (`pg_try_advisory_xact_lock_shared`, non-blocking).
  - If the try fails, the writer rolls back with a non-failing busy outcome: the run is not marked failed, autopipe records it as skipped, and the next tick retries.
  - Before each `compress_chunk`, compression takes the session-level exclusive fence, waiting no longer than a configured bound that is charged against the chunk's compress timeout. While waiting it holds no other lock, so the wait happens **before** any copy.
- **Contention is not a failure.** A chunk whose fence wait times out is recorded as `deferred_contended`. It does not make the tick `partial`, and the receipt and exit code distinguish it from a real failure.
- Runbook and lifecycle-lock docstring updated.

## Impact
- Files:
  - `scripts/node27_timeseries_compression.py`
  - `workers/output_parser/parser.py`
  - the forcing writers of compressed hypertables, if they are compressed
  - a new small shared module under `packages/common/`
  - tests, `docs/runbooks/tier-node27-timeseries-storage.md`
  - `packages/common/node27_timeseries_lifecycle_lock.py` (docstring only)
- Operational cost, made explicit: while one chunk compresses (about 20–35 min, at most 2 chunks a day), ingest writes on that hypertable defer to later ticks instead of failing. This is the daily quiet window.
- No schema migration.
