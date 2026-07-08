-- Canonical Grid Snapshot identity partial-unique index (SUB-5 / issue #902)
--
-- Backs the SUB-5 writer's application-level "find-then-insert" idempotency
-- check with a DB-level constraint so concurrent writers cannot race past
-- the check-window and land duplicate ``(source_id, grid_id, grid_signature)``
-- rows for the same active snapshot. The partial predicate
-- ``WHERE superseded_at IS NULL`` keeps historical superseded rows out of
-- the constraint so a legitimate re-registration under a NEW snapshot (with
-- the same identity triple after the historical row was superseded) still
-- succeeds — supersession is a SUB-9 lifecycle concern; identity-uniqueness
-- applies to the ACTIVE row set only.
--
-- The application layer (SUB-5 writer) still runs the find-first-then-insert
-- pattern for the common non-racing path; this index is the concurrency
-- backstop. On UniqueViolation the writer re-queries via
-- ``find_snapshot_by_identity`` and returns the winning row's id (idempotent
-- from the caller's perspective — read-your-writes under concurrency).

CREATE UNIQUE INDEX IF NOT EXISTS uq_canonical_grid_snapshot_identity_active
    ON met.canonical_grid_snapshot (source_id, grid_id, grid_signature)
    WHERE superseded_at IS NULL;
