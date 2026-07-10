-- Epic #982 change `mapping-variant-state-compatibility` D2: fingerprint-gated
-- state-clone provenance columns on hydro.state_snapshot. At cutover from a
-- legacy model M0 to a direct-grid variant M1 the mechanism clones the latest
-- qualified (M0, source, t*) snapshot row into (M1, source, t*) — only when the
-- M0 and M1 hydrologic_core_fingerprint values are equal — so M1's first strict
-- cycle finds an exact successor state without a physical file copy
-- (fingerprint-gated-state-clone capability).
--
-- The three columns record `cloned_from` provenance on the clone row: the
-- source snapshot identity (`cloned_from_state_id`), the source `model_id`
-- (`cloned_from_model_id` = M0), and the gating `hydrologic_core_fingerprint`
-- value that permitted the clone (`clone_gate_fingerprint`). All three are
-- NULLable with default NULL so pre-clone / legacy snapshot rows keep their
-- existing identity, remain selectable by the unchanged warm-start path, and
-- are not rewritten. Same column-only NULL-default house style as
-- 000028_state_lineage.sql; no data backfill, no index change, no drop of the
-- existing `(model_id, COALESCE(source_id, ''), valid_time)` unique index
-- `state_snapshot_model_source_valid_time_key` (which remains the authority
-- for per-source warm-state identity).

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS cloned_from_state_id TEXT DEFAULT NULL;

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS cloned_from_model_id TEXT DEFAULT NULL;

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS clone_gate_fingerprint TEXT DEFAULT NULL;
