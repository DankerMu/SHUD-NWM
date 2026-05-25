DO $$
BEGIN
  ALTER TABLE ops.pipeline_job
    ADD COLUMN IF NOT EXISTS manual_retry_marker BOOLEAN NOT NULL DEFAULT false;
END $$;

UPDATE ops.pipeline_job
SET manual_retry_marker = true
WHERE manual_retry_marker IS false
  AND run_id IS NOT NULL
  AND substr(job_id, 1, length(run_id || '_retry_')) = run_id || '_retry_';

CREATE UNIQUE INDEX IF NOT EXISTS pipeline_job_active_manual_retry_guard_idx
  ON ops.pipeline_job (run_id)
  WHERE manual_retry_marker IS true
    AND run_id IS NOT NULL
    AND status IN ('pending', 'queued', 'submitted', 'running');
