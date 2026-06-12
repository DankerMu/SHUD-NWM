CREATE TABLE IF NOT EXISTS flood.run_product_quality (
  run_id TEXT PRIMARY KEY REFERENCES hydro.hydro_run(run_id) ON DELETE CASCADE,
  result_rows BIGINT NOT NULL DEFAULT 0 CHECK (result_rows >= 0),
  max_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (max_result_rows >= 0),
  return_period_rows BIGINT NOT NULL DEFAULT 0 CHECK (return_period_rows >= 0),
  warning_rows BIGINT NOT NULL DEFAULT 0 CHECK (warning_rows >= 0),
  max_return_period_rows BIGINT NOT NULL DEFAULT 0 CHECK (max_return_period_rows >= 0),
  max_warning_rows BIGINT NOT NULL DEFAULT 0 CHECK (max_warning_rows >= 0),
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TimescaleDB hypertables do not support CREATE INDEX CONCURRENTLY.
CREATE INDEX IF NOT EXISTS return_period_result_null_return_period_run_idx
  ON flood.return_period_result (run_id)
  WHERE return_period IS NULL;

CREATE INDEX IF NOT EXISTS return_period_result_null_warning_level_run_idx
  ON flood.return_period_result (run_id)
  WHERE warning_level IS NULL;
