# Bound the lock wait of `compress_chunk` and alert when the compression unit fails

## Why

On 2026-11-19 the node-27 compression tick started `compress_chunk` on a
45 GB river chunk at 04:25:00Z. The data rewrite finished after about eight
minutes; the statement then produced nothing until `statement_timeout`
cancelled it at 05:25:00Z, and the unit exited 1 (#3380, found while
investigating #3234). Two facts measured on 2026-11-25:

- **The wait blocked readers.** That day's PostgreSQL log has eight
  "canceling statement due to statement timeout" lines: the compression
  statement and seven read queries, at 04:47, 05:00, 05:02 and 05:17Z. On
  2026-11-18, 20, 23 and 24 the count is zero for the whole day.
- **The mechanism, reproduced on TimescaleDB 2.10.2** (throwaway instance):
  after rewriting the data, `compress_chunk` takes `AccessExclusiveLock` on
  the uncompressed chunk. An unfinished transaction that holds
  `ACCESS SHARE` on the chunk or its hypertable makes it wait, and every
  reader that arrives afterwards queues behind the waiting request. With
  `lock_timeout` set, the statement ends with SQLSTATE 55P03 after that
  time, the chunk stays uncompressed, and the queued reader returns at
  once.

The compression session sets `statement_timeout` only. The compression/ingest
fence does set `lock_timeout`, but for its own advisory lock, and resets it
before the compress statement runs. The unit has no `OnFailure=`, so the
failed tick of 2026-11-19 sent no alert.

## What changes

- **The compress statement runs under a short `lock_timeout`**, its own
  setting (`NODE27_TIMESERIES_COMPRESSION_LOCK_TIMEOUT_MS`, default 5000),
  set after the fence is held and before `compress_chunk`.
- **A lock-wait timeout is contention, not failure.** After the same
  read-only reconcile every failure gets, the chunk is recorded as
  `deferred_contended` with `contended_on: "relation_lock"`; the tick goes
  on to its next chunk and can end `deferred` (exit 0). A statement timeout
  stays a failure.
- **Receipt schema 2.3**: `budget.lock_timeout_ms`, and `contended_on` on
  every `deferred_contended` chunk (`fence`, `relation_lock`,
  `tick_deadline`), so the three reasons a chunk was put off are told apart
  by a field and not by the absence of another one.
- **The compression unit declares `OnFailure=`** on the node-27
  unit-failure alert, as the governance and retention units do.

## What does not change

- Selection (newest eligible first, table order, `per_tick_bound`), the lag,
  the budget chain legs and their validation, the tick deadline guard.
- `acquire_compression_fence` and its tests; the fence wait budget.
- The reconcile-before-label rule for a failed `compress_chunk`.
- Receipts of schema 1.0 through 2.2 keep validating; the frozen
  live-evidence contracts are untouched.
- An env file that does not declare the new key keeps working, so the
  deployment needs no edit of the live env file on node-27.
- The four forcing chunks that were skipped in November and the question of
  catching up put-off chunks (item 1 of #3380) are a separate slice; see
  "Not in this change".

## Decisions

- **D1 Its own setting, not the fence wait.** The fence wait (900 s) is an
  advisory lock that blocks nobody. A relation-lock wait blocks readers for
  as long as it lasts, so its budget is a different quantity and two orders
  of magnitude smaller.
- **D2 Default 5 s, in code.** In a healthy tick the lock is granted at
  once. The display role runs with `statement_timeout = '30s'`
  (`docs/runbooks/display-readonly-live-mvt.md`): a reader that queues at
  the start of a 30 s wait would itself be cancelled, which is the
  2026-11-19 symptom at small scale. Five seconds tolerates a reader that
  is about to finish and keeps a queued reader well inside its own limit. A
  default in code (as the fence wait has; only `per_tick_bound` is
  mandatory without one) lets the change deploy by `git pull` alone.
- **D3 Strictly below `compress_timeout_ms - fence_wait_ms`, as a sanity
  bound.** `lock_timeout` times one lock wait from the moment that wait
  starts; `statement_timeout` runs from the start of the statement, and the
  wait comes after the rewrite. A rewrite that uses most of the budget can
  therefore still end in a statement timeout while waiting. The two
  outcomes are told apart by their SQLSTATE (55P03 against 57014), not by
  which timer is shorter; the bound only refuses a configuration in which
  the lock timeout could never matter.
- **D4 Contention, exit 0.** The chunk was not harmed, the blocker is
  outside the runner, and the tick's other chunks still have budget. The
  fence capability's requirement "Contended deferral is distinguishable
  from failure" today counts only fence contention; it is modified to count
  a relation-lock timeout too. The price is stated under "Known limits".
- **D5 Translate at the statement, classify in the loop.** The default
  compress function turns a `pgcode == "55P03"` error raised by the
  compress statement into `RelationLockContended`, a runner exception that
  is not a subclass of `FenceContended`; the tick loop reconciles and
  labels. The fence keeps raising `FenceContended` from its own call, so
  the two 55P03 sources cannot be mixed up.
- **D6 `contended_on` on all three deferral kinds.** A tick-deadline
  deferral is recognised today by a missing `fence_wait_elapsed_ms`; with a
  third kind that convention stops being readable.
- **D7 stderr stays in its file.** Redirecting it to the journal would make
  the alert mail carry the error text, but changes where every existing
  runbook step looks. The runbook says where to look instead.

## Known limits

- The rewrite that precedes the lock request is thrown away on a lock
  timeout (about eight minutes for a river chunk today). The chunk is
  attempted again when it is next selected.
- A lock that conflicts at the start of `compress_chunk` (an anti-wraparound
  autovacuum on the chunk, a manual `VACUUM` or `ANALYZE`) also ends as a
  `relation_lock` deferral, with no rewrite lost.
- A chunk that is put off by contention is, on the next day, no longer the
  newest eligible one. Whether it is selected again depends on the bound:
  that is the selection gap of #3380 item 1, which this change does not
  close. Until it is, a contended river chunk can stay uncompressed (about
  45 GB) and the working-set governance audit is what shows it.
- A persistent blocker produces `deferred` ticks without an alert. The
  receipt and the governance audit show it; nobody is mailed.
- The alert mail quotes the unit's journal, which holds only systemd's own
  lines for this unit.
- Who held the lock on 2026-11-19 is not known: `log_lock_waits` is off on
  the production instance. Turning it on is an operator decision outside
  this change.

## Not in this change

Item 1 of #3380 (put-off chunks are not selected again). Measured on
2026-11-25: a river chunk of 42 GB plus a forcing chunk of 2.7 GB take 9 to
17 minutes per tick, against 55 s/GB in the runbook's capacity derivation
(taken before the 2026-11-05 disk change). At the measured rates three
chunks fit the wall, so raising `PER_TICK_BOUND` from 2 to 3 would drain a
backlog without new runner code; it needs the capacity derivation redone,
the template, pin test and spec updated, and an edit of the live env file,
which is the owner's call.

## Impact

- `scripts/node27_timeseries_compression.py`,
  `schemas/timeseries_compression_receipt.schema.json` and its example,
  `infra/env/node27-timeseries-compression.example`,
  `infra/systemd/nhms-node27-timeseries-compression.service`,
  `infra/systemd/nhms-node27-unit-failure-alert@.service` (comment).
- Tests: the runner's unit tests, the schema tests, the unit-file pin, one
  real-database test (`integration`, `timescaledb_210`).
- Docs: `docs/runbooks/tier-node27-timeseries-storage.md`.
- Spec: `hypertable-compression` (two requirements added, one modified);
  `timeseries-compression-ingest-fence` (one requirement modified).
