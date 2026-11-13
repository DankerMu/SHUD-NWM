# Design — provenance-batch-journal-reuse

## D1. Reuse the repository, not a new memo

The repository's byte cache is already the reviewed, stat-validated mechanism (#1734 D11 accounting, identity-retry
on atomic replacement). Passing one instance through the batch reuses it with zero new invalidation logic. A
cycle-level memo of the publication view (decoded rows keyed by cycle_id) was rejected: it needs its own invalidation
proof against concurrent cohort writers and would duplicate the cache's contract.

## D2. Root binding is checked, not assumed

`publish_run_pipeline_job_provenance(repository=...)` must refuse a repository whose root differs from the verified
`journal_root` (raise `PipelineJobProvenanceError`, code e.g. `JOURNAL_REPOSITORY_ROOT_MISMATCH`), so a caller cannot
publish one root's rows under another's authority. Compare resolved paths the same way the repository stores its
root.

## D3. Thread safety

Within a pass, the batch runs inside one cohort thread; other cohort threads have their own batches and repositories.
The shared instance is therefore used by one thread at a time; the cache already takes `_cache_lock`. No new locking.

## D4. Memory

Cycle journal replay is ~13.7 MB per run on node-22; the cache bound (64 MiB) holds it. If a future cycle exceeds the
bound, entries are evicted and reads fall back to disk — slower, never wrong.

## Risks

- A cached decoded flag (`_read_bytes_limited_cached` returns "already validated") skips re-validation only for
  byte-identical content, so validation semantics are unchanged.
