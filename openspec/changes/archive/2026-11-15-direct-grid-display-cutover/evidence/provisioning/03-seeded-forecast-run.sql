-- Task 4.1(d) — seed a pre-cutover synthetic forecast run so the basin is
-- discoverable in the `/` basin selector via `GET /api/v1/basins?has_display_product=true`.
--
-- `has_display_product` (packages/common/model_registry.py:810-848) resolves
-- basins that have at least one `hydro.hydro_run` matching:
--   - `run_type = 'forecast'`
--   - `status ∈ QHH_LATEST_READY_RUN_STATUSES` (succeeded / parsed / published)
--   - `cycle_time IS NOT NULL`
--
-- The rehearsal seeds ONE such row bound to the baseline evidence
-- `model_instance` (`model__evidence_cmfd_p02_synth__v1`), with `cycle_time`
-- 24h earlier than the rehearsal window so that opening a new M1 cell-station
-- pin on this cycle hits the retention empty state (the station-series file
-- for that old cycle does not exist).
--
-- Cleanup: `rehearse.py`'s restore section deletes this row by run_id.

BEGIN;

-- Idempotent skip if the seeded run already exists.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM hydro.hydro_run WHERE run_id = 'run__evidence_cmfd_p02_synth__rehearsal_pre_cutover_v1'
  ) THEN
    RAISE NOTICE 'IDEMPOTENT SKIP: seeded pre-cutover forecast run already exists.';
    RETURN;
  END IF;
END $$;

-- Cycle time: 24h before now(). rehearse.py verifies (via MAX(created_at))
-- that no NEW hydro_run row is created during the rehearsal window, so
-- using `now() - interval '24 hours'` at provisioning time is safe.
INSERT INTO hydro.hydro_run (
  run_id,
  run_type,
  scenario_id,
  model_id,
  basin_version_id,
  source_id,
  cycle_time,
  start_time,
  end_time,
  status,
  run_manifest_uri
) VALUES (
  'run__evidence_cmfd_p02_synth__rehearsal_pre_cutover_v1',
  'forecast',
  'evidence-rehearsal',
  'model__evidence_cmfd_p02_synth__v1',
  'basin__evidence_cmfd_p02_synth__v1',
  'gfs',
  date_trunc('hour', now() - interval '24 hours'),
  date_trunc('hour', now() - interval '24 hours'),
  date_trunc('hour', now() - interval '24 hours') + interval '72 hours',
  'succeeded',
  'https://github.com/DankerMu/SHUD-NWM/tree/master/openspec/changes/direct-grid-display-cutover/evidence#seeded-pre-cutover-cycle'
)
ON CONFLICT (run_id) DO NOTHING;

-- Post-check: exactly 1 seeded run row for the evidence basin.
DO $$
BEGIN
  IF (SELECT count(*) FROM hydro.hydro_run
        WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
          AND model_id = 'model__evidence_cmfd_p02_synth__v1'
          AND status = 'succeeded'
          AND run_type = 'forecast'
          AND cycle_time IS NOT NULL) < 1 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: expected at least 1 seeded pre-cutover forecast run';
  END IF;
END $$;

COMMIT;

-- Report seeded row and cycle_time for the pass log.
SELECT run_id, model_id, basin_version_id, source_id, cycle_time, status, run_type
FROM hydro.hydro_run
WHERE run_id = 'run__evidence_cmfd_p02_synth__rehearsal_pre_cutover_v1';
