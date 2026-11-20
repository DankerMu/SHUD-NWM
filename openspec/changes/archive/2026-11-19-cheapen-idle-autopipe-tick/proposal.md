# Make the idle node-27 autopipeline tick cheap and its log bounded (#3224)

## Why

`nhms-node27-autopipe` runs every 10 minutes. A tick with nothing to ingest spends 218–223 s in `phase=ingest`
(six idle ticks measured 2026-11-19T23:04Z .. 2026-11-20T01:08Z) and appends 877 KB to `autopipe.log`
(518 MB on 2026-11-20, last truncated by hand on 2026-11-13, no rotation, same volume as PGDATA).

One idle invocation was profiled on node-27 under the timer's own lock on 2026-11-20T00:47Z
(`/home/nwm/tmp/autopipe-prof-20261120T0041Z/`, 3,564 runs discovered, all `already_ingested`, 244 s with the profiler):

| share | what |
| --- | --- |
| 220.5 s (90.7 %) | `import_run_pipeline_job_provenance`, called for all 3,564 already-ingested runs |
| of which 75 s | a new PostgreSQL connection per run |
| of which 79 s | 46,584 statements (`_load_hydro_identity` 3,564×, `_fetch_existing_job` 21,341×, advisory locks) |
| of which 48 s | `_validate_imported_job` for 21,341 jobs |
| 10.5 s | `_ensure_seeded_basin_display_ready` for 66 basins |
| ~12 s | discovery, manifest reads, seed checks, summary |

The pass changed nothing: across the 3,564 runs it reported `inserted 0, updated 0, unchanged 21,287, ignored_stale 54`.
The sidecars are not rewritten either: the newest sidecar mtime was 1.9 h old, 0 of 3,564 changed in the last 30
minutes, 132 in the last 6 h (one cycle, two sources). Reading and hashing all 3,564 sidecars (18.5 MB) took 0.15 s on
the same host; through the safe object-store reader the profile shows 4.5 s.

The log: 727 KB of the 877 KB is `runs.job_provenance.runs`, one entry per run saying `unchanged`.

So the idle tick is not looking for new cycles. It re-proves every tick that 21,341 job rows still equal 3,564 files
that have not changed.

## What changes

1. **Provenance import skips a run whose evidence is unchanged.** The discovered pass keeps a small state file
   (per run: SHA-256 of the sidecar bytes last imported, and for every job of that sidecar a fingerprint of its
   `ops.pipeline_job` row as read back after that import). A run is skipped, without a transaction, only when its
   sidecar bytes hash to the recorded digest **and** every recorded job row is still present with that same
   fingerprint (one batched read per pass). Everything else is imported exactly as today. The state is an optimisation only: missing, corrupt, or
   contradicted by the database (rows lost, or replaced by other versions, as after a restore), it is ignored for the
   affected runs and they go through today's import.
2. **A tick summary mode for the timer.** `--summary-mode tick` prints one single-line JSON digest per tick and prints
   the full summary after it only when the tick did or found something (the predicate is pinned in the spec). The
   default (`full`) keeps today's stdout: one pretty-printed JSON document. The wrapper passes `tick`.
3. **Size-bounded rotation in the wrapper**, under the tick lock, before `autopipe: start`.

`design.md` is present (fixture level `expanded`).

## What does not change

- One transaction and one connection per imported run (failure isolation between runs). Connection reuse is not
  needed once unchanged runs are skipped: a cycle changes 132 sidecars, about 3 s of connects inside a 2,000–3,000 s
  ingest tick.
- The explicit bounded import (`import_runs_pipeline_job_provenance`, the backfill CLI): it never reads or writes the
  state file.
- The 10-minute cadence, the phases after ingest, the per-phase `elapsed_sec` lines, stderr.
- `_ensure_seeded_basin_display_ready` (10.5 s per tick): recorded here, not touched.
- No schema change, no migration.

## Observed values of the quiet predicate on an idle tick

From the profiled idle summary (`stdout.log` in the profile directory) and the timer log of the same hour:
`return_code 0`; `runs.ingested / skipped / failed / declined = 0 / 0 / 0 / 0`; `seed.seeded 0`, `seed.failed 0`,
`seed.activation_skipped_other_active 0` (66 `already_seeded`); `declines_active 0` (13 of the last 13 ticks);
`stats_guard.status not_triggered`, `stats_guard.authority = {status: completed, analyzed: [], deferred: []}`.
`job_provenance` was `imported 3564` only because nothing is skipped today. So every term of the predicate holds on
the idle ticks this change is about. A term that turns persistently true (an active decline, an authority table that
stays a candidate) makes every tick print the full summary; with unchanged provenance collapsed to a count that
summary would have been 877 KB less the 727 KB run list plus indentation, about 35 KB pretty-printed on the profiled tick, i.e. about 5 MB a day, inside the
rotation bound.

Bytes of one idle tick in the timer log today (2026-11-20T01:26Z): ingest invocation 877,333; coverage backstop 323;
MVT prewarm 1,104.

## Triage

```text
Issue type: refactor
Fixture level: expanded
Upstream suggested level: absent
Blast radius: job provenance rows in ops.pipeline_job go stale or missing for runs wrongly skipped; operators lose
  the failing run's stage/reason in autopipe.log; a rotation bug loses tick output
Selected risk packs: Public API / CLI / script entry; Config / project setup; File IO / path safety / overwrite;
  Schema / columns / units / field names; Concurrency / shared state / ordering; Resource limits / large input /
  discovery; Legacy compatibility / examples; Error handling / rollback / partial outputs; Documentation / migration notes
Evidence floor: E1-E6 in tasks.md (named pytest files locally and on node-27 with real PostgreSQL; measured idle
  elapsed_sec and ingest-phase log bytes on node-27; one real ingest tick)
```
