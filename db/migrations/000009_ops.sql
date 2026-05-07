CREATE TABLE IF NOT EXISTS ops.pipeline_job (
  job_id TEXT PRIMARY KEY,
  run_id TEXT,
  cycle_id TEXT,
  job_type TEXT NOT NULL,
  slurm_job_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  stage TEXT,
  submitted_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  exit_code INT,
  retry_count INT NOT NULL DEFAULT 0,
  error_code TEXT,
  error_message TEXT,
  log_uri TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pipeline_job_run_idx ON ops.pipeline_job (run_id);
CREATE INDEX IF NOT EXISTS pipeline_job_cycle_idx ON ops.pipeline_job (cycle_id);

CREATE TABLE IF NOT EXISTS ops.pipeline_event (
  event_id BIGSERIAL PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  status_from TEXT,
  status_to TEXT,
  message TEXT,
  details JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pipeline_event_entity_idx ON ops.pipeline_event (entity_type, entity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ops.qc_result (
  qc_id BIGSERIAL PRIMARY KEY,
  qc_checkpoint TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  run_id TEXT,
  cycle_id TEXT,
  passed BOOLEAN NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  checks_json JSONB NOT NULL,
  message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qc_result_target_idx ON ops.qc_result (target_type, target_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ops.audit_log (
  log_id BIGSERIAL PRIMARY KEY,
  actor TEXT NOT NULL,
  actor_role TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}',
  ip_address INET,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_entity_idx ON ops.audit_log (entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_log_actor_idx ON ops.audit_log (actor, created_at DESC);
