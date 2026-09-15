## Context

Live read-only facts (2026-09-15T02:52Z, node-27, `READ ONLY` transactions):

| Fact | Value |
|---|---|
| Legacy routes, `end_time` within 21 days | 3692 `published` + 78 `superseded`; all 7-day runs; cycles 2026-08-18..09-14 |
| Legacy routes, aged out | 2646 `published` + 228 `superseded` |
| Estimated rows to write | 3.56 B (Σ segment_count × 168); largest network 16 009 segments = 2.69 M rows/run |
| Narrow bytes/row (live, uncompressed incl. indexes) | ≈179 B → ≈0.63 TB uncompressed before compression catches up |
| Headroom | `/data/GHDC` (PGDATA) 13 T free; `/` 19 G; RAM 69 G available, 40 cores |
| WAL | `archive_mode=off`, no replication slots, `max_wal_size` 1 GB |
| Narrow chunks | eight 1-day chunks 2026-08-19..08-27, uncompressed; compression lag 48 h, tick 04:25 UTC |
| Legacy chunks | five 7-day chunks 2026-08-20..09-24; retention drops them like narrow chunks (`_legacy` sibling) |
| Parser throughput (production autopipe, NEW) | one LH-YLJ run: 512 232 rows in ≈34 s, one process |
| Since T0 | only LH-YLJ (3049 segments) has gone through the NEW parser in production |

At ~15 k rows/s per process, a single-process run needs ~66 h. The runner is therefore concurrent and resumable. The
production pilot measures real scaling before the full run.

## Goals / Non-Goals

Goals: every in-window legacy-routed run is readable from the narrow store with the same values; no instant where a
route points at a store without the run's facts; safe stop/resume; typed receipts; a contract gate that does not wait
for legacy chunks to age out.

Non-goals: reparsing aged-out runs; touching the legacy table; parser/reader changes; the contract migration itself
(#1988); deploying master to production.

## Decisions

### D1 One run, one transaction

The runner opens its own connection. In one transaction it:

1. Takes `SELECT … FOR UPDATE` on the `hydro_run` row.
2. Re-checks the route, status, `parsed_at` and the window against `now()`.
3. Runs `UPDATE timeseries_store='narrow'`.
4. Calls `OutputParser.parse_run` with `PsycopgOutputParserRepository(_connection=conn)`. That repository's
   `transaction()` yields without committing, so the parser's narrow upsert, QC row and `mark_run_parsed` join the
   runner's transaction.
5. Checks the result: narrow `count(*)` for the `run_key` equals `rows_written` (> 0), the status is unchanged and the
   route is `narrow`.
6. Commits.

Any exception, or any failed check, rolls back. The route, facts, status and `parsed_at` are then exactly as before.
Autopipe on the same run serializes on the row lock (and refuses legacy routes anyway).

### D2 Scope is the retention window, evaluated per run

The candidate predicate is: route `legacy`, `parsed_at IS NOT NULL`, status `published`/`superseded`, and
`end_time > now() - NODE27_TIMESERIES_RETENTION_WINDOW_DAYS`. The window uses `end_time`, not `cycle_time`, so a run
whose tail is still visible is backfilled whole. Candidates are ordered newest cycle first, so a deadline stop keeps
the most valuable work. The runner re-checks the predicate under the row lock, so a run that ages out mid-run is
skipped as `aged_out`. `--end-time-after` narrows the scope further when the contract date is known: runs that age
out before the contract do not need a backfill.

The parser's status gate cannot demote these statuses: `mark_run_parsed` updates status only from
`succeeded/parsed/failed`, and `mark_run_failed` only from FAILABLE statuses (neither contains `published` or
`superseded`).

### D3 Aged-out runs stay `legacy`

Flipping an aged-out run to `narrow` would claim facts that do not exist. They keep `legacy`, and their facts leave
with retention or with the contract's DROP, which is the same visibility loss retention already imposes. The contract
gate (parent amendment) becomes **zero legacy-routed runs inside the retention window**. The fourteen daily receipts
are unchanged.

### D4 Lifecycle mutex for the whole run; decompress overlapping narrow chunks

Almost every candidate writes `valid_time` older than the 48 h compression lag. A compression tick during the backfill
would compress target chunks, and `check_batch_targets_uncompressed` would then fail the next writes. The runner
therefore holds `/tmp/nhms-node27-timeseries-lifecycle.lock` for its whole life. Compression and retention ticks exit
refused while it runs; they are the designed contenders, and autopipe stays outside the mutex. Under the lock, the
runner decompresses (as table owner `nhms_ingest_rw`) every compressed narrow chunk that overlaps
`[min start_time, max end_time]` of the candidates. It refuses if their pre-compression size exceeds
`--max-decompress-bytes` (20 GiB default). The legacy table is never decompressed. After the run releases the lock,
the next compression tick compresses the backlog.

### D5 Concurrency, stop and resume

`--concurrency N` runs N worker processes, one connection each. Workers ignore SIGINT/SIGTERM. The coordinator stops
dispatching on a signal, at `--deadline`, or once `--max-failures` runs have failed. In-flight runs finish, or are
killed and roll back. Each outcome is appended with fsync to `runs.jsonl`. Candidates are re-selected from the
database on every start, so a rerun resumes naturally and never repeats a committed run (it is `narrow` now). Exit
codes: 0 complete, 1 any failure, 2 refused (nothing mutated), 3 partial (deadline or signal).

### D6 Oracles

In-transaction count equality (D1) is the per-run gate. `verify` compares legacy and narrow values for sampled
reparsed runs by joining text identities to keys (value, unit, quality flag, lead time). It is diagnostic where legacy
chunks still exist. Before commit, the source of truth is the SHUD artifact the parser just read.

### D7 Provenance and deployment

The runner imports the parser from the tree it runs in. For production, the exact reviewed bytes of the script are
published to `~/.local/state/issue1987-tools/<commit>/` and run with NEW `415cbd1e`'s interpreter and
`PYTHONPATH=/home/nwm/NWM`. The summary records `tool_sha256` and `parser_sha256`. DSNs come only from the ingest env
file, built in memory by a launcher; they never appear in argv or receipts.

## Risks / Trade-offs

- [Throughput lower than estimated] → pilot `--limit` with concurrency 4 first; `--deadline` bounds each session;
  resume is free.
- [Compression/retention refused for the run's duration; uncompressed working set grows ≈0.6 TB] → 13 T headroom; the
  lock is released at the end and the backlog compresses on the next tick; the governance receipt is checked after
  the run.
- [Checkpoint/WAL pressure at `max_wal_size` 1 GB] → throughput-only effect; no archive or slot can fill a disk.
- [A reparse differs from the legacy parse] → `verify` sample; any mismatch stops the rollout before the contract.
- [Autopipe latency during the run] → bounded concurrency; autopipe is not blocked by the mutex.
