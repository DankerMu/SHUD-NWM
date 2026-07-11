-- Task 4.1(b) canonical grid snapshot for the synthetic M1 target.
--
-- The M1 target is registered via `workers.model_registry.direct_grid_variant_registration.
-- register_direct_grid_variant`, which resolves `met.canonical_grid_snapshot`
-- either by explicit `grid_snapshot_id` OR by `(grid_signature, grid_id)`.
-- This file inserts that snapshot row so `02-register-direct-grid-variant.py`
-- resolves it deterministically.
--
-- source_id choice
-- ----------------
-- `met.canonical_grid_snapshot.source_id` is a HARD FK to `met.data_source`;
-- node-27 only has `IFS`/`gfs` rows in `met.data_source` (no `cmfd`).
-- Additionally, `workers/forcing_producer/direct_grid_contract.py::
-- _applicable_source_ids` normalizes each contract source_id via
-- `packages.common.source_identity.normalize_source_id`, which only accepts
-- {GFS, ERA5, IFS} — `cmfd` is rejected as unsupported. We therefore use
-- `source_id='gfs'` here AND declare `applicable_source_ids=['gfs']`.
-- The `cmfd` narrative is preserved in the basin_version_id / baseline
-- model_id / seeded run_id identifiers, not in the parser-touched source
-- scope. The clone hook's per-source dispatch iterates over the M1 target's
-- stored `applicable_source_ids` (source_scope on the activation context),
-- so the target's contract must declare a normalize_source_id-accepted value.

BEGIN;

-- Pre-check: expect exactly 2 canonical_grid_snapshot rows on node-27 (IFS + gfs).
-- Idempotent skip if this rehearsal grid snapshot already exists.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM met.canonical_grid_snapshot
     WHERE grid_id = 'synth-grid-p0.2-m1-v2'
       AND grid_signature = 'e2c0bf1a8d6c4f5b9a7f3e1d0c8b6a4f2d7e5c3b1a9f8d6c4b2a0f9e7d5c3b1a9'
  ) THEN
    RAISE NOTICE 'IDEMPOTENT SKIP: rehearsal canonical_grid_snapshot already exists.';
    RETURN;
  END IF;
END $$;

-- Insert the synthetic M1 grid snapshot. Tiny bbox matches the synthetic
-- basin polygon (99.9 29.9, 100.6 30.6) from register-synth-p02.sql line 31.
INSERT INTO met.canonical_grid_snapshot (
  canonical_grid_key,
  source_id,
  grid_id,
  grid_signature,
  grid_definition_uri,
  grid_definition_checksum,
  longitude_convention,
  latitude_order,
  flatten_order,
  native_resolution,
  bbox_south,
  bbox_north,
  bbox_west,
  bbox_east,
  converter_version,
  valid_from,
  applicable_source_ids
) VALUES (
  'canonical__evidence_cmfd_p02_synth__m1_v2',
  'gfs',
  'synth-grid-p0.2-m1-v2',
  'e2c0bf1a8d6c4f5b9a7f3e1d0c8b6a4f2d7e5c3b1a9f8d6c4b2a0f9e7d5c3b1a9',
  'https://github.com/DankerMu/SHUD-NWM/tree/master/openspec/changes/direct-grid-display-cutover/evidence/provisioning/synthetic-package',
  'ac31d4e0d6b81cb1f0a5e2f3d9c8b6a4f2d7e5c3b1a9f8d6c4b2a0f9e7d5c3b1a',
  'lon_0_360',
  'north_to_south',
  'row_major',
  0.5,
  29.9,
  30.6,
  99.9,
  100.6,
  'evidence-only-v1',
  now(),
  ARRAY['gfs']::TEXT[]
);

-- Post-check: exactly 1 new grid snapshot row for the rehearsal identity.
DO $$
BEGIN
  IF (SELECT count(*) FROM met.canonical_grid_snapshot
        WHERE grid_id = 'synth-grid-p0.2-m1-v2') != 1 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: expected exactly 1 rehearsal grid snapshot row';
  END IF;
END $$;

COMMIT;

-- Report the resolved grid_snapshot_id (UUID) for downstream provisioning.
SELECT grid_snapshot_id, canonical_grid_key, grid_id, grid_signature, applicable_source_ids
FROM met.canonical_grid_snapshot
WHERE grid_id = 'synth-grid-p0.2-m1-v2';
