CREATE INDEX IF NOT EXISTS hydro_run_ops_strict_identity_candidates_idx
  ON hydro.hydro_run (source_id, cycle_time, run_id, model_id);
