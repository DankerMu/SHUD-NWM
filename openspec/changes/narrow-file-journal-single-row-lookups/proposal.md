# Narrow the file journal's single-row lookups to the cycle that owns the row

## Why

`_iter_pipeline_job_records()` (`services/orchestrator/file_orchestration_journal.py:4839`)
is an unindexed full-table scan: it reads **every** `latest/**/*.json` view and
**every** `journal/**/*.jsonl` segment and JSON-decodes all of it, on every call.
Six public entrypoints call it to answer what are semantically single-row
lookups (`:1053`, `:1061`, `:1099`, `:1154`, `:1166`, `:1176`). The only defence
is a per-file byte cache whose FIFO limits (`:154-155`) were chosen when
`latest/` was far smaller; a cyclic full-tree scan is the pathological input for
FIFO.

Measured on node-22 (issue #1734, and re-measured at `e9970a1b` on
2026-08-22 during pass `scheduler_2026082219_f048457a8e0d`, 14 candidates,
elapsed 20:50 / CPU 18:22):

```
rchar      11,238,986,932   (11.24 GB logical read)
syscr             358,198
read_bytes  1,010,974,720   (1.01 GB REAL disk IO)
syscw                  31
```

Working set: `latest/` 3,979 files / 444.3 MiB, `journal/` 231 segments /
121.8 MiB, total **566.1 MiB** against a 64 MiB byte cap — **8.8x** — and
3,979 files against a 4,096-entry cap, i.e. **97.1%** of the entry cap. That is
roughly **20 full-tree replays in 21 minutes**, with `syscw = 31`: writes cannot
explain any of it.

The original measurement recorded `read_bytes = 6.2 MB` and concluded the
amplification was purely logical, absorbed by page cache. **That mitigation is
gone**: `read_bytes` is now 1.01 GB, a 163x increase, because the working set
has outgrown what the page cache retains between scans. The full-tree replay now
issues real NFS reads. The cost grows as
`queries x (cycles x models-per-cycle x ~117 KB + per-cycle jsonl)`; all three
factors grow monotonically with no pruning (62 days retained, registry 34 -> 48
models), and the scheduler interval is 300 s.

## What Changes

- A **cycle-scoped record iteration**: `_iter_pipeline_job_records` gains a
  cycle-scoped sibling that replays exactly the same record sources — that
  cycle's `latest/<source>/<cycle>/**` views, that cycle's
  `journal/<source>/<cycle>.jsonl` segments, and direct records — through the
  same merge path, in the same order, with the same blocked-row error shape.
  It is a *narrowing of the input set only*, never a different merge.
- A **key -> (source, cycle) derivation** built from the existing run-id/path
  helpers (never a fresh parser), applied to the entrypoints whose argument
  carries a cycle: `query_pipeline_jobs_by_cycle`, `query_pipeline_jobs_by_run`,
  `query_candidate_state` / `_candidate_job_for_idempotency_unlocked` (key is
  `run_id:stage`), and `_pipeline_job_for_id_unlocked`.
- **Fall open, never fall closed**: when the cycle cannot be derived with
  certainty, the entrypoint falls back to today's full scan. A narrowed lookup
  that misses a row is the silent direction (a missed dedup hit double-submits;
  a missed reconcile row mints a wrong retry), so uncertainty resolves to
  slow-but-correct, never to "not found".
- `query_pipeline_job_by_slurm_id` **keeps the full scan** — its argument
  carries no derivable cycle, and minting a persisted by-slurm index is new
  durable state plus a backfill, out of proportion to this change.
- A recorded **retention ruling** for `latest/` / `journal/` (the working-set
  upper bound the issue's acceptance criteria require), with implementation
  routed to a follow-up issue rather than executed here.

## Non-Goals

- **`_cycle_rows` write-window global clear is not touched** — #1658 owns that;
  it is a different cache face, and `syscw = 31` proves writes cannot account
  for this read volume.
- **state-index / registry manifest / canonical-readiness load paths are not
  touched** — #1734 disproved the "re-read per candidate" hypothesis for all
  three (each is a once-per-pass `refresh()` + `_load_once()`); touching them
  would be pure risk.
- **`strict_warm_start_evidence`'s whole-state read + SHA-256 is not touched** —
  #1542's surface; neither the read-size fingerprint nor the fd sampling points
  at it.
- **The FIFO byte cache's policy and limits are not changed.** Raising them or
  switching to LRU is the issue's *fallback*: it pushes the crossing point out
  without repairing the growth law, and costs a 3,979-file `stat` sweep per
  fingerprint. Narrowing repairs the growth law itself.
- **Query semantics do not change.** No entrypoint gains or loses a row; the
  DB lane (`chain_repository.py`, SQL-indexed) has no analogous copy and is
  untouched.
- **Retention is ruled, not implemented, here.** Pruning touches live rows —
  including #1748's wedged reservation rows — and belongs in its own change.
