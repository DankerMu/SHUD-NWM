-- Manual retry writes hydro.hydro_run.status='pending' while the retry job is queued.
-- Keep the production enum aligned with the state exposed by retry APIs.
ALTER TYPE hydro.run_status ADD VALUE IF NOT EXISTS 'pending' BEFORE 'submitted';

-- Cancel operations write met.forecast_cycle.current_state='cancelled' for active cycles.
-- Keep the production enum aligned with the terminal cancellation state.
ALTER TYPE met.cycle_status ADD VALUE IF NOT EXISTS 'cancelled' AFTER 'failed_publish';
