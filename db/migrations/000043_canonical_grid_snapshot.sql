-- Canonical Source Grid Registry (SUB-2 / issue #899)
--
-- Adds the immutable Grid Snapshot registry backing the canonical-source-grid-registry
-- OpenSpec change. See openspec/changes/canonical-source-grid-registry/design.md §1
-- for the append-only storage decision and §7 for the derived-cache supersession
-- ownership boundary. The migration is additive and idempotent.
--
-- Tables:
--   met.canonical_grid_snapshot  -- one immutable row per registered grid
--   met.canonical_grid_cell      -- ordered per-cell geometry
--
-- Column additions:
--   met.canonical_met_product.grid_snapshot_id (nullable FK)
--   met.met_station.superseded_at, met.met_station.grid_snapshot_id
--   met.interp_weight.active_flag, met.interp_weight.superseded_at,
--     met.interp_weight.grid_snapshot_id
--
-- Triggers (identity + display cross-check enforcement):
--   canonical_met_product_grid_definition_uri_match_trg    -- URI-match on insert/update
--   canonical_grid_snapshot_identity_immutable_trg         -- reject identity mutation
--   canonical_grid_cell_immutable_trg                      -- reject any UPDATE on cell rows
--   canonical_grid_cell_direct_delete_blocked_trg          -- reject direct DELETE (cascade allowed)
--
-- DELETE-rejection on met.canonical_grid_snapshot itself is a store-layer concern
-- deferred to Task 2.2 (SUB-3) — this migration provides UPDATE-based identity
-- enforcement, and ON DELETE CASCADE on met.canonical_grid_cell is intentional to
-- support store-driven snapshot removal during future spec-driven migrations.

CREATE TABLE IF NOT EXISTS met.canonical_grid_snapshot (
  grid_snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_grid_key TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES met.data_source(source_id),
  grid_id TEXT NOT NULL,
  grid_signature TEXT NOT NULL,
  grid_definition_uri TEXT NOT NULL,
  grid_definition_checksum TEXT NOT NULL,
  longitude_convention TEXT NOT NULL,
  latitude_order TEXT NOT NULL,
  flatten_order TEXT NOT NULL,
  native_resolution DOUBLE PRECISION NOT NULL,
  bbox_south DOUBLE PRECISION NOT NULL,
  bbox_north DOUBLE PRECISION NOT NULL,
  bbox_west DOUBLE PRECISION NOT NULL,
  bbox_east DOUBLE PRECISION NOT NULL,
  converter_version TEXT NOT NULL,
  valid_from TIMESTAMPTZ NOT NULL,
  valid_to TIMESTAMPTZ NULL,
  applicable_source_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  superseded_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS met.canonical_grid_cell (
  grid_snapshot_id UUID NOT NULL REFERENCES met.canonical_grid_snapshot(grid_snapshot_id) ON DELETE CASCADE,
  grid_cell_id TEXT NOT NULL,
  longitude DOUBLE PRECISION NOT NULL,
  latitude DOUBLE PRECISION NOT NULL,
  canonical_ordinal INTEGER NOT NULL CHECK (canonical_ordinal >= 1),
  PRIMARY KEY (grid_snapshot_id, grid_cell_id),
  UNIQUE (grid_snapshot_id, canonical_ordinal)
);

-- Nullable FK linking product-instance rows to the immutable snapshot; the snapshot
-- is the single referential-integrity anchor (design.md §1).
ALTER TABLE met.canonical_met_product
  ADD COLUMN IF NOT EXISTS grid_snapshot_id UUID REFERENCES met.canonical_grid_snapshot(grid_snapshot_id);

-- Derived-cache staleness columns (design.md §7 supersession ownership boundary).
-- met.met_station already carries active_flag from 000005_met.sql:54; add the two
-- new staleness columns only.
ALTER TABLE met.met_station
  ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ NULL;
ALTER TABLE met.met_station
  ADD COLUMN IF NOT EXISTS grid_snapshot_id UUID REFERENCES met.canonical_grid_snapshot(grid_snapshot_id);

-- met.interp_weight has no active_flag / superseded_at / grid_snapshot_id yet; add
-- all three. active_flag defaults true so pre-existing rows load without backfill.
ALTER TABLE met.interp_weight
  ADD COLUMN IF NOT EXISTS active_flag BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE met.interp_weight
  ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ NULL;
ALTER TABLE met.interp_weight
  ADD COLUMN IF NOT EXISTS grid_snapshot_id UUID REFERENCES met.canonical_grid_snapshot(grid_snapshot_id);

-- Trigger A: URI-match on met.canonical_met_product insert/update.
--
-- When a product row carries both a grid_snapshot_id FK and a grid_definition_uri
-- display/cross-check field, the URI MUST match the snapshot's grid_definition_uri
-- (grid-snapshot-registration/spec.md scenario "Grid definitions are not stored
-- independently in both tables"). The FK constraint already guarantees the
-- snapshot row exists; this trigger enforces the cross-check field agreement.
CREATE OR REPLACE FUNCTION met.canonical_met_product_grid_definition_uri_match()
RETURNS TRIGGER AS $$
DECLARE
  snapshot_uri TEXT;
BEGIN
  -- The NULL-URI branch is intentional — when a product row's display cross-check
  -- URI is unset (or explicitly cleared), no cross-check comparison is possible.
  -- The FK column on `grid_snapshot_id` remains the referential-integrity anchor.
  IF NEW.grid_snapshot_id IS NULL OR NEW.grid_definition_uri IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT grid_definition_uri
    INTO snapshot_uri
    FROM met.canonical_grid_snapshot
    WHERE grid_snapshot_id = NEW.grid_snapshot_id;

  IF snapshot_uri IS NULL THEN
    -- Referential integrity is enforced by the FK; if we get here the referenced
    -- row is missing which the FK will reject on commit. Do not silently accept.
    RETURN NEW;
  END IF;

  IF snapshot_uri <> NEW.grid_definition_uri THEN
    RAISE EXCEPTION
      'canonical_met_product.grid_definition_uri (%) does not match snapshot % grid_definition_uri (%)',
      NEW.grid_definition_uri, NEW.grid_snapshot_id, snapshot_uri;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS canonical_met_product_grid_definition_uri_match_trg
  ON met.canonical_met_product;
CREATE TRIGGER canonical_met_product_grid_definition_uri_match_trg
  BEFORE INSERT OR UPDATE ON met.canonical_met_product
  FOR EACH ROW
  EXECUTE FUNCTION met.canonical_met_product_grid_definition_uri_match();

-- Trigger B: Reject any UPDATE that mutates identity fields on
-- met.canonical_grid_snapshot. Only superseded_at and applicable_source_ids are
-- permitted post-insert writes (design.md §7 + Task 5.1 acceptance;
-- grid-snapshot-registration/spec.md scenario "Snapshot grid_definition_uri
-- cannot be modified by canonical_met_product inserts";
-- grid-drift-lifecycle/spec.md scenario "Registry API rejects in-place signature
-- replacement").
CREATE OR REPLACE FUNCTION met.canonical_grid_snapshot_identity_immutable()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.grid_signature IS DISTINCT FROM OLD.grid_signature THEN
    RAISE EXCEPTION
      'canonical_grid_snapshot identity field grid_signature is immutable (snapshot %)',
      OLD.grid_snapshot_id;
  END IF;

  IF NEW.grid_definition_uri IS DISTINCT FROM OLD.grid_definition_uri THEN
    RAISE EXCEPTION
      'canonical_grid_snapshot identity field grid_definition_uri is immutable (snapshot %)',
      OLD.grid_snapshot_id;
  END IF;

  IF NEW.grid_definition_checksum IS DISTINCT FROM OLD.grid_definition_checksum THEN
    RAISE EXCEPTION
      'canonical_grid_snapshot identity field grid_definition_checksum is immutable (snapshot %)',
      OLD.grid_snapshot_id;
  END IF;

  IF NEW.canonical_grid_key IS DISTINCT FROM OLD.canonical_grid_key THEN
    RAISE EXCEPTION
      'canonical_grid_snapshot identity field canonical_grid_key is immutable (snapshot %)',
      OLD.grid_snapshot_id;
  END IF;

  IF NEW.bbox_south IS DISTINCT FROM OLD.bbox_south
     OR NEW.bbox_north IS DISTINCT FROM OLD.bbox_north
     OR NEW.bbox_west IS DISTINCT FROM OLD.bbox_west
     OR NEW.bbox_east IS DISTINCT FROM OLD.bbox_east THEN
    RAISE EXCEPTION
      'canonical_grid_snapshot identity field bbox is immutable (snapshot %)',
      OLD.grid_snapshot_id;
  END IF;

  IF NEW.native_resolution IS DISTINCT FROM OLD.native_resolution THEN
    RAISE EXCEPTION
      'canonical_grid_snapshot identity field native_resolution is immutable (snapshot %)',
      OLD.grid_snapshot_id;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS canonical_grid_snapshot_identity_immutable_trg
  ON met.canonical_grid_snapshot;
CREATE TRIGGER canonical_grid_snapshot_identity_immutable_trg
  BEFORE UPDATE ON met.canonical_grid_snapshot
  FOR EACH ROW
  EXECUTE FUNCTION met.canonical_grid_snapshot_identity_immutable();

-- Trigger C: Reject any UPDATE on met.canonical_grid_cell. Per-cell rows are
-- identity components of the immutable snapshot
-- (grid-snapshot-registration/spec.md scenario "Snapshots are never updated in
-- place" enumerates "per-cell rows" alongside grid_signature and bbox). Cells
-- are structurally immutable once inserted; any drift in geometry requires
-- registering a new snapshot version (grid-drift-lifecycle capability).
CREATE OR REPLACE FUNCTION met.canonical_grid_cell_immutable()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION
    'canonical_grid_cell rows are immutable once inserted (grid_snapshot_id=%, grid_cell_id=%); register a new snapshot version to change cell geometry',
    OLD.grid_snapshot_id, OLD.grid_cell_id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS canonical_grid_cell_immutable_trg
  ON met.canonical_grid_cell;
CREATE TRIGGER canonical_grid_cell_immutable_trg
  BEFORE UPDATE ON met.canonical_grid_cell
  FOR EACH ROW
  EXECUTE FUNCTION met.canonical_grid_cell_immutable();

-- Trigger D: Reject DIRECT DELETE on met.canonical_grid_cell while allowing
-- CASCADE-driven deletes from met.canonical_grid_snapshot. Postgres sets
-- pg_trigger_depth() = 1 when the DELETE is a top-level statement and
-- pg_trigger_depth() > 1 when it originates from a foreign-key CASCADE. This
-- preserves the intended parent-snapshot-driven removal path while blocking
-- ad-hoc DELETE FROM met.canonical_grid_cell WHERE ... from callers.
CREATE OR REPLACE FUNCTION met.canonical_grid_cell_direct_delete_blocked()
RETURNS TRIGGER AS $$
BEGIN
  IF pg_trigger_depth() = 1 THEN
    RAISE EXCEPTION
      'canonical_grid_cell rows may not be deleted directly (grid_snapshot_id=%, grid_cell_id=%); DELETE the parent canonical_grid_snapshot row instead to cascade',
      OLD.grid_snapshot_id, OLD.grid_cell_id;
  END IF;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS canonical_grid_cell_direct_delete_blocked_trg
  ON met.canonical_grid_cell;
CREATE TRIGGER canonical_grid_cell_direct_delete_blocked_trg
  BEFORE DELETE ON met.canonical_grid_cell
  FOR EACH ROW
  EXECUTE FUNCTION met.canonical_grid_cell_direct_delete_blocked();
