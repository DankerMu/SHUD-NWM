-- Task 4.1(a) recorded bypass — provision the synthetic previous-active
-- baseline for the display-cutover rehearsal.
--
-- Rationale (design.md §6 phase 1):
--   The rehearsed lifecycle operation is the cutover (Change 4 `activate` on
--   the M1 target), NOT the baseline. So this file provisions the baseline
--   `core.model_instance` row and the legacy-shaped `synth-station-001..003`
--   rows into an `active` display state by recorded SQL — a documented
--   bypass whose cleanup is recorded in restore/README.md.
--
-- Non-goal:
--   Zero production row is touched. The `model__evidence%` and
--   `basin__evidence%` identifiers are non-production by design (see
--   `openspec/changes/archive/2026-07-10-cmfd-direct-grid-platform-readiness/evidence/register-synth-p02.sql`).
--
-- Pre-check ↔ post-check symmetry:
--   Pre: 13 production active `model_instance` rows, 1 evidence
--        `model__evidence_cmfd_p02_synth__v1` row `active_flag=false`,
--        3 `synth-station-001..003` rows `active_flag=false`.
--   Post: 14 active `model_instance` rows (13 production + 1 baseline
--        evidence), 3 synthetic stations `active_flag=true`.

BEGIN;

-- Pre-check: baseline state must be exactly the readiness archive snapshot.
DO $$
BEGIN
  IF (SELECT count(*) FROM core.model_instance WHERE active_flag = true) != 13 THEN
    RAISE EXCEPTION 'PRECHECK FAIL: expected 13 production active model_instance rows, got %',
      (SELECT count(*) FROM core.model_instance WHERE active_flag = true);
  END IF;
  IF (SELECT count(*) FROM core.model_instance
        WHERE model_id = 'model__evidence_cmfd_p02_synth__v1'
          AND active_flag = false) != 1 THEN
    RAISE EXCEPTION 'PRECHECK FAIL: evidence baseline model_instance row not in expected inactive state';
  END IF;
  IF (SELECT count(*) FROM met.met_station
        WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
          AND station_id IN ('synth-station-001','synth-station-002','synth-station-003')
          AND active_flag = false) != 3 THEN
    RAISE EXCEPTION 'PRECHECK FAIL: expected 3 synth-station-* rows in active_flag=false, got %',
      (SELECT count(*) FROM met.met_station
         WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
           AND station_id IN ('synth-station-001','synth-station-002','synth-station-003')
           AND active_flag = false);
  END IF;
END $$;

-- Flip the baseline evidence model_instance row to `active`. This row is the
-- "previous active model" the cutover transaction reads from
-- `_fetch_active_model_for_scope`; the flip hook then re-points its display
-- set (the 3 synth-station rows below) onto the M1 target's mirror.
UPDATE core.model_instance
   SET active_flag = true,
       lifecycle_state = 'active'
 WHERE model_id = 'model__evidence_cmfd_p02_synth__v1';

-- Flip the 3 legacy-shaped synthetic stations to `active_flag=true`. The
-- flip hook's step-1 UPDATE (`SET active_flag=false WHERE basin_version_id=%s
-- AND active_flag=true`) will re-point them off during the cutover
-- transaction; restore/README.md documents the cleanup.
UPDATE met.met_station
   SET active_flag = true
 WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
   AND station_id IN ('synth-station-001','synth-station-002','synth-station-003');

-- Post-check: expected during-provision state.
DO $$
BEGIN
  IF (SELECT count(*) FROM core.model_instance WHERE active_flag = true) != 14 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: global active model_instance count expected 14, got %',
      (SELECT count(*) FROM core.model_instance WHERE active_flag = true);
  END IF;
  IF (SELECT count(*) FROM core.model_instance
        WHERE model_id = 'model__evidence_cmfd_p02_synth__v1'
          AND active_flag = true
          AND lifecycle_state = 'active') != 1 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: evidence baseline model_instance row not flipped active';
  END IF;
  IF (SELECT count(*) FROM met.met_station
        WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
          AND station_id IN ('synth-station-001','synth-station-002','synth-station-003')
          AND active_flag = true) != 3 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: expected 3 synth-station-* rows in active_flag=true';
  END IF;
  -- Production-scoped invariant (excluding evidence identifiers).
  IF (SELECT count(*) FROM core.model_instance
        WHERE active_flag = true
          AND model_id NOT LIKE 'model__evidence%') != 13 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: production-scoped active count changed; expected 13';
  END IF;
END $$;

COMMIT;

-- Report row identifiers for the pass log.
SELECT model_id, basin_version_id, active_flag, lifecycle_state
FROM core.model_instance
WHERE model_id = 'model__evidence_cmfd_p02_synth__v1';

SELECT station_id, basin_version_id, active_flag, station_role, grid_snapshot_id
FROM met.met_station
WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
ORDER BY station_id;
