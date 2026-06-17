CREATE TABLE IF NOT EXISTS flood.run_product_quality (
  run_id TEXT PRIMARY KEY REFERENCES hydro.hydro_run(run_id) ON DELETE CASCADE,
  quality_state TEXT NOT NULL DEFAULT 'ready' CHECK (quality_state IN ('ready', 'degraded', 'unavailable')),
  quality_source TEXT NOT NULL DEFAULT 'historical_backfill'
    CHECK (quality_source IN ('historical_backfill', 'explicit')),
  unavailable_products JSONB NOT NULL DEFAULT '[]'::jsonb,
  residual_blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
  result_rows BIGINT NOT NULL DEFAULT 0 CHECK (result_rows >= 0),
  max_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (max_result_rows >= 0),
  return_period_rows BIGINT NOT NULL DEFAULT 0 CHECK (return_period_rows >= 0),
  warning_rows BIGINT NOT NULL DEFAULT 0 CHECK (warning_rows >= 0),
  max_return_period_rows BIGINT NOT NULL DEFAULT 0 CHECK (max_return_period_rows >= 0),
  max_warning_rows BIGINT NOT NULL DEFAULT 0 CHECK (max_warning_rows >= 0),
  expected_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (expected_result_rows >= 0),
  expected_max_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (expected_max_result_rows >= 0),
  expected_timestep_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (expected_timestep_result_rows >= 0),
  meaningful_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (meaningful_result_rows >= 0),
  meaningful_max_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (meaningful_max_result_rows >= 0),
  meaningful_timestep_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (meaningful_timestep_result_rows >= 0),
  no_frequency_curve_rows BIGINT NOT NULL DEFAULT 0 CHECK (no_frequency_curve_rows >= 0),
  no_usable_frequency_curve_rows BIGINT NOT NULL DEFAULT 0 CHECK (no_usable_frequency_curve_rows >= 0),
  warning_threshold_unavailable_rows BIGINT NOT NULL DEFAULT 0 CHECK (warning_threshold_unavailable_rows >= 0),
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT run_product_quality_unavailable_products_array_chk
    CHECK (jsonb_typeof(unavailable_products) = 'array'),
  CONSTRAINT run_product_quality_residual_blockers_array_chk
    CHECK (jsonb_typeof(residual_blockers) = 'array')
);
