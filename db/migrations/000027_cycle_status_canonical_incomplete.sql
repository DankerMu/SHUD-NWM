-- The canonical converter persists met.forecast_cycle.status='canonical_incomplete'
-- when a cycle converts but fails readiness (missing leads/variables or rejected
-- quality). The enum omitted this value, so the write raised
-- "invalid input value for enum met.cycle_status: canonical_incomplete" and the
-- cycle was force-marked failed_convert instead. Add the value the code already
-- uses, ordered next to its sibling canonical state.
ALTER TYPE met.cycle_status ADD VALUE IF NOT EXISTS 'canonical_incomplete' AFTER 'canonical_ready';
