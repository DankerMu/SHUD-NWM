ALTER TABLE flood.run_product_quality
  ADD COLUMN IF NOT EXISTS quality_state TEXT NOT NULL DEFAULT 'ready'
    CHECK (quality_state IN ('ready', 'degraded', 'unavailable')),
  ADD COLUMN IF NOT EXISTS quality_source TEXT NOT NULL DEFAULT 'historical_backfill'
    CHECK (quality_source IN ('historical_backfill', 'explicit')),
  ADD COLUMN IF NOT EXISTS unavailable_products JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS residual_blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS expected_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (expected_result_rows >= 0),
  ADD COLUMN IF NOT EXISTS expected_max_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (expected_max_result_rows >= 0),
  ADD COLUMN IF NOT EXISTS expected_timestep_result_rows BIGINT NOT NULL DEFAULT 0
    CHECK (expected_timestep_result_rows >= 0),
  ADD COLUMN IF NOT EXISTS meaningful_result_rows BIGINT NOT NULL DEFAULT 0 CHECK (meaningful_result_rows >= 0),
  ADD COLUMN IF NOT EXISTS meaningful_max_result_rows BIGINT NOT NULL DEFAULT 0
    CHECK (meaningful_max_result_rows >= 0),
  ADD COLUMN IF NOT EXISTS meaningful_timestep_result_rows BIGINT NOT NULL DEFAULT 0
    CHECK (meaningful_timestep_result_rows >= 0),
  ADD COLUMN IF NOT EXISTS no_frequency_curve_rows BIGINT NOT NULL DEFAULT 0 CHECK (no_frequency_curve_rows >= 0),
  ADD COLUMN IF NOT EXISTS no_usable_frequency_curve_rows BIGINT NOT NULL DEFAULT 0
    CHECK (no_usable_frequency_curve_rows >= 0),
  ADD COLUMN IF NOT EXISTS warning_threshold_unavailable_rows BIGINT NOT NULL DEFAULT 0
    CHECK (warning_threshold_unavailable_rows >= 0);

UPDATE flood.run_product_quality
SET unavailable_products = '[]'::jsonb
WHERE jsonb_typeof(unavailable_products) <> 'array';

UPDATE flood.run_product_quality
SET residual_blockers = '[]'::jsonb
WHERE jsonb_typeof(residual_blockers) <> 'array';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'flood.run_product_quality'::regclass
      AND conname = 'run_product_quality_unavailable_products_array_chk'
  ) THEN
    ALTER TABLE flood.run_product_quality
      ADD CONSTRAINT run_product_quality_unavailable_products_array_chk
      CHECK (jsonb_typeof(unavailable_products) = 'array');
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'flood.run_product_quality'::regclass
      AND conname = 'run_product_quality_residual_blockers_array_chk'
  ) THEN
    ALTER TABLE flood.run_product_quality
      ADD CONSTRAINT run_product_quality_residual_blockers_array_chk
      CHECK (jsonb_typeof(residual_blockers) = 'array');
  END IF;
END $$;

WITH source_quality AS (
  SELECT
    run_id,
    COUNT(*) AS result_rows,
    SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END) AS max_result_rows,
    SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) AS return_period_rows,
    SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END) AS warning_rows,
    SUM(CASE WHEN max_over_window = true AND return_period IS NOT NULL THEN 1 ELSE 0 END)
      AS max_return_period_rows,
    SUM(CASE WHEN max_over_window = true AND warning_level IS NOT NULL THEN 1 ELSE 0 END)
      AS max_warning_rows,
    SUM(CASE WHEN quality_flag = 'no_frequency_curve' THEN 1 ELSE 0 END)
      AS no_frequency_curve_rows,
    SUM(CASE WHEN quality_flag = 'no_usable_frequency_curve' THEN 1 ELSE 0 END)
      AS no_usable_frequency_curve_rows,
    SUM(CASE WHEN quality_flag = 'warning_thresholds_unavailable' THEN 1 ELSE 0 END)
      AS warning_threshold_unavailable_rows
  FROM flood.return_period_result
  GROUP BY run_id
),
derived AS (
  SELECT
    quality.run_id,
    COALESCE(source.result_rows, quality.result_rows) AS result_rows,
    COALESCE(source.max_result_rows, quality.max_result_rows) AS max_result_rows,
    COALESCE(source.return_period_rows, quality.return_period_rows) AS return_period_rows,
    COALESCE(source.warning_rows, quality.warning_rows) AS warning_rows,
    COALESCE(source.max_return_period_rows, quality.max_return_period_rows) AS max_return_period_rows,
    COALESCE(source.max_warning_rows, quality.max_warning_rows) AS max_warning_rows,
    CASE
      WHEN COALESCE(source.no_frequency_curve_rows, 0) + COALESCE(source.no_usable_frequency_curve_rows, 0) > 0
        THEN COALESCE(source.no_frequency_curve_rows, 0)
      ELSE GREATEST(COALESCE(source.result_rows, quality.result_rows)
        - COALESCE(source.return_period_rows, quality.return_period_rows), 0)
    END AS no_frequency_curve_rows,
    COALESCE(source.no_usable_frequency_curve_rows, quality.no_usable_frequency_curve_rows, 0)
      AS no_usable_frequency_curve_rows,
    CASE
      WHEN COALESCE(source.warning_threshold_unavailable_rows, 0) > 0
        THEN COALESCE(source.warning_threshold_unavailable_rows, 0)
      ELSE GREATEST(COALESCE(source.return_period_rows, quality.return_period_rows)
        - COALESCE(source.warning_rows, quality.warning_rows), 0)
    END AS warning_threshold_unavailable_rows
  FROM flood.run_product_quality AS quality
  LEFT JOIN source_quality AS source
    ON (source.run_id = quality.run_id)
),
backfill AS (
  SELECT
    derived.*,
    GREATEST(derived.return_period_rows, derived.warning_rows) AS meaningful_result_rows,
    GREATEST(derived.max_return_period_rows, derived.max_warning_rows) AS meaningful_max_result_rows,
    GREATEST(
      GREATEST(derived.return_period_rows, derived.warning_rows)
        - GREATEST(derived.max_return_period_rows, derived.max_warning_rows),
      0
    ) AS meaningful_timestep_result_rows,
    CASE
      WHEN derived.return_period_rows <= 0 THEN 'unavailable'
      WHEN derived.warning_threshold_unavailable_rows > 0 THEN 'unavailable'
      WHEN derived.no_frequency_curve_rows + derived.no_usable_frequency_curve_rows > 0 THEN 'degraded'
      ELSE 'ready'
    END AS quality_state,
    to_jsonb(array_remove(ARRAY[
      CASE WHEN derived.return_period_rows <= 0 THEN 'return_period_result' ELSE NULL END,
      CASE
        WHEN derived.no_frequency_curve_rows + derived.no_usable_frequency_curve_rows > 0
          THEN 'frequency_curves'
        ELSE NULL
      END,
      CASE WHEN derived.warning_threshold_unavailable_rows > 0 THEN 'warning_thresholds' ELSE NULL END
    ], NULL)) AS unavailable_products,
    (
      CASE
        WHEN derived.return_period_rows <= 0 THEN jsonb_build_array(jsonb_build_object(
          'code', 'RETURN_PERIOD_RESULT_UNAVAILABLE',
          'state', 'unavailable',
          'quality_flag', 'missing_return_period_result',
          'residual_risk', 'No non-null return-period rows are available for this run.',
          'run_id', derived.run_id,
          'count', derived.result_rows
        ))
        ELSE '[]'::jsonb
      END
      || CASE
        WHEN derived.no_frequency_curve_rows > 0 THEN jsonb_build_array(jsonb_build_object(
          'code', 'FREQUENCY_CURVES_UNAVAILABLE',
          'state', CASE WHEN derived.return_period_rows <= 0 THEN 'unavailable' ELSE 'degraded' END,
          'quality_flag', 'no_frequency_curve',
          'residual_risk', 'Some rows have null return_period because frequency curves are unavailable.',
          'run_id', derived.run_id,
          'count', derived.no_frequency_curve_rows
        ))
        ELSE '[]'::jsonb
      END
      || CASE
        WHEN derived.no_usable_frequency_curve_rows > 0 THEN jsonb_build_array(jsonb_build_object(
          'code', 'FREQUENCY_CURVES_UNAVAILABLE',
          'state', CASE WHEN derived.return_period_rows <= 0 THEN 'unavailable' ELSE 'degraded' END,
          'quality_flag', 'no_usable_frequency_curve',
          'residual_risk', 'Some rows have null return_period because frequency curves are unusable.',
          'run_id', derived.run_id,
          'count', derived.no_usable_frequency_curve_rows
        ))
        ELSE '[]'::jsonb
      END
      || CASE
        WHEN derived.warning_threshold_unavailable_rows > 0 THEN jsonb_build_array(jsonb_build_object(
          'code', 'WARNING_THRESHOLDS_UNAVAILABLE',
          'state', 'unavailable',
          'quality_flag', 'warning_thresholds_unavailable',
          'residual_risk', 'warning_level remains null for return-period rows.',
          'run_id', derived.run_id,
          'count', derived.warning_threshold_unavailable_rows
        ))
        ELSE '[]'::jsonb
      END
    ) AS residual_blockers
  FROM derived
)
UPDATE flood.run_product_quality AS quality
SET
  expected_result_rows = CASE WHEN quality.expected_result_rows = 0 THEN backfill.result_rows ELSE quality.expected_result_rows END,
  expected_max_result_rows = CASE
    WHEN quality.expected_max_result_rows = 0 THEN backfill.max_result_rows
    ELSE quality.expected_max_result_rows
  END,
  expected_timestep_result_rows = CASE
    WHEN quality.expected_timestep_result_rows = 0 THEN GREATEST(backfill.result_rows - backfill.max_result_rows, 0)
    ELSE quality.expected_timestep_result_rows
  END,
  meaningful_result_rows = CASE
    WHEN quality.meaningful_result_rows = 0 THEN backfill.meaningful_result_rows
    ELSE quality.meaningful_result_rows
  END,
  meaningful_max_result_rows = CASE
    WHEN quality.meaningful_max_result_rows = 0 THEN backfill.meaningful_max_result_rows
    ELSE quality.meaningful_max_result_rows
  END,
  meaningful_timestep_result_rows = CASE
    WHEN quality.meaningful_timestep_result_rows = 0 THEN backfill.meaningful_timestep_result_rows
    ELSE quality.meaningful_timestep_result_rows
  END,
  quality_state = backfill.quality_state,
  no_frequency_curve_rows = backfill.no_frequency_curve_rows,
  no_usable_frequency_curve_rows = backfill.no_usable_frequency_curve_rows,
  warning_threshold_unavailable_rows = backfill.warning_threshold_unavailable_rows,
  unavailable_products = (
    SELECT COALESCE(to_jsonb(array_agg(product ORDER BY product)), '[]'::jsonb)
    FROM (
      SELECT DISTINCT product
      FROM (
        SELECT jsonb_array_elements_text(quality.unavailable_products) AS product
        UNION ALL
        SELECT jsonb_array_elements_text(backfill.unavailable_products) AS product
      ) AS product_union
      WHERE product <> ''
    ) AS normalized_products
  ),
  residual_blockers = (
    COALESCE(
      (
        SELECT jsonb_agg(existing.blocker)
        FROM jsonb_array_elements(quality.residual_blockers) AS existing(blocker)
        WHERE NOT (
          existing.blocker->>'code' IN (
            'RETURN_PERIOD_RESULT_UNAVAILABLE',
            'FREQUENCY_CURVES_UNAVAILABLE',
            'WARNING_THRESHOLDS_UNAVAILABLE'
          )
          AND existing.blocker->>'run_id' = quality.run_id
          AND existing.blocker->>'quality_flag' IN (
            'missing_return_period_result',
            'no_frequency_curve',
            'no_usable_frequency_curve',
            'warning_thresholds_unavailable'
          )
        )
      ),
      '[]'::jsonb
    ) || backfill.residual_blockers
  )
FROM backfill
WHERE quality.run_id = backfill.run_id
  AND quality.quality_source <> 'explicit'
  AND (
    quality.expected_result_rows = 0
    OR quality.expected_max_result_rows = 0
    OR quality.expected_timestep_result_rows = 0
    OR quality.meaningful_result_rows = 0
    OR quality.meaningful_max_result_rows = 0
    OR quality.meaningful_timestep_result_rows = 0
    OR quality.no_frequency_curve_rows = 0
    OR quality.no_usable_frequency_curve_rows = 0
    OR quality.warning_threshold_unavailable_rows = 0
    OR quality.unavailable_products = '[]'::jsonb
    OR quality.residual_blockers = '[]'::jsonb
  );
