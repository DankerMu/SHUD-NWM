DO $$
BEGIN
  CREATE TYPE hydro.run_type AS ENUM ('analysis', 'forecast', 'hindcast');
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;

DO $$
BEGIN
  CREATE TYPE hydro.run_status AS ENUM (
    'created',
    'staged',
    'submitted',
    'running',
    'succeeded',
    'parsed',
    'frequency_done',
    'published',
    'failed',
    'cancelled',
    'superseded'
  );
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;

DO $$
BEGIN
  CREATE TYPE met.source_status AS ENUM ('enabled', 'restricted', 'planned', 'mock', 'deprecated');
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;

DO $$
BEGIN
  CREATE TYPE met.cycle_status AS ENUM (
    'discovered',
    'downloading',
    'raw_complete',
    'canonical_ready',
    'forcing_ready_partial',
    'forcing_ready',
    'forecast_running',
    'parsed_partial',
    'complete',
    'published',
    'failed_download',
    'failed_convert',
    'failed_forcing',
    'failed_run',
    'failed_parse',
    'failed_publish'
  );
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;
