-- M24 §3A: durable two-phase reservation columns on ops.pipeline_job.
--
-- Concurrent submit-and-return writes a durable reservation (status='reserved')
-- inside the pass lock BEFORE sbatch, then atomically binds slurm_job_id on
-- submit. A stable idempotency_key per candidate+stage guards against
-- double-submit across overlapping passes and across the submit-crash window
-- (crash after sbatch accepts but before the slurm_job_id bind).
--
-- All columns are NULLable with default NULL so existing rows are untouched and
-- pre-reservation producers (which do not supply these fields) keep working.

ALTER TABLE ops.pipeline_job
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT DEFAULT NULL;

ALTER TABLE ops.pipeline_job
  ADD COLUMN IF NOT EXISTS candidate_id TEXT DEFAULT NULL;

-- Partial unique index: at most one durable row per idempotency_key. The
-- WHERE clause keeps legacy NULL rows (pre-reservation) from colliding, so this
-- is a forward-only upgrade safe to apply over existing data.
CREATE UNIQUE INDEX IF NOT EXISTS pipeline_job_idempotency_key_uidx
  ON ops.pipeline_job (idempotency_key)
  WHERE idempotency_key IS NOT NULL;
