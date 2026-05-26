DO $$
BEGIN
  ALTER TABLE ops.pipeline_job
    ADD COLUMN IF NOT EXISTS manual_retry_marker BOOLEAN NOT NULL DEFAULT false;
END $$;

WITH ranked_active_legacy_retries AS (
  SELECT
    job_id,
    row_number() OVER (
      PARTITION BY run_id
      ORDER BY
        submitted_at DESC NULLS LAST,
        created_at DESC NULLS LAST,
        updated_at DESC NULLS LAST,
        finished_at DESC NULLS LAST,
        job_id DESC
    ) AS retry_rank
  FROM ops.pipeline_job
  WHERE manual_retry_marker IS false
    AND run_id IS NOT NULL
    AND status IN ('pending', 'queued', 'submitted', 'running')
    AND substr(job_id, 1, length(run_id || '_retry_')) = run_id || '_retry_'
)
UPDATE ops.pipeline_job AS job
SET manual_retry_marker = true
FROM ranked_active_legacy_retries AS ranked
WHERE job.job_id = ranked.job_id
  AND ranked.retry_rank = 1;

CREATE UNIQUE INDEX IF NOT EXISTS pipeline_job_active_manual_retry_guard_idx
  ON ops.pipeline_job (run_id)
  WHERE manual_retry_marker IS true
    AND run_id IS NOT NULL
    AND status IN ('pending', 'queued', 'submitted', 'running');
