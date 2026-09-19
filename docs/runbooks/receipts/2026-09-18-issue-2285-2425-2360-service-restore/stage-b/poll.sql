SELECT now()::timestamp(0), a.pid, a.wait_event_type, a.wait_event, date_trunc('second', now()-a.query_start), pg_blocking_pids(a.pid),
 (SELECT string_agg(b.application_name || '/' || coalesce(date_trunc('second', now()-b.xact_start)::text, '-'), ',') FROM pg_stat_activity b WHERE b.pid = ANY(pg_blocking_pids(a.pid))),
 left(a.query, 70)
FROM pg_stat_activity a WHERE a.application_name = 'nhms-ts-compression' AND a.state <> 'idle';
