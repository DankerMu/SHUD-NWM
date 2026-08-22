-- Change `recalibration-state-carryover` D6: record WHICH gate admitted a
-- fingerprint-gated state clone. Migration 000046 added the three
-- `cloned_from_*` / `clone_gate_fingerprint` provenance columns, but a clone
-- row could not say which surface set that fingerprint covered. This change
-- adds a second, narrower admission route -- the eight-surface
-- state-compatibility gate for a calibration-only M1 -> M1' update -- so the
-- accepted fingerprint value alone is no longer self-describing.
--
-- `clone_gate_kind` names the admitting gate:
--   'hydrologic_core'      -- the ten-surface hydrologic_core_fingerprint gate
--                             (transfer_mode='fix_forward', the default route)
--   'state_compatibility'  -- the eight-surface state-compatibility gate
--                             (transfer_mode='recalibration')
-- `clone_gate_fingerprint` records the accepted value OF THAT SAME GATE, so
-- the pair (clone_gate_kind, clone_gate_fingerprint) is self-describing and an
-- auditor never compares two values computed over different surface sets.
--
-- NULLable with default NULL and no backfill, so every pre-existing clone row
-- and every ordinary forecast / save-state row keeps its identity unchanged,
-- remains selectable by the unchanged warm-start and lineage-validation paths,
-- and is not rewritten. Same column-only NULL-default house style as
-- 000046_state_snapshot_clone_provenance.sql; no index change and no drop of
-- the `(model_id, COALESCE(source_id, ''), valid_time)` unique index
-- `state_snapshot_model_source_valid_time_key`, which remains the authority
-- for per-source warm-state identity.

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS clone_gate_kind TEXT DEFAULT NULL;
