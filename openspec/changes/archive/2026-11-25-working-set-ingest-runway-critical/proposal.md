# The working-set critical becomes an ingest runway (#3379)

## Why

`PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE` fired on node-27 five times in thirteen days (2026-11-13,
11-20, 11-22, 11-24, 11-25). Read-only investigation of the 45 stored governance receipts (#3379):

- The projection is `uncompressed + daily_ingest × days until the oldest uncompressed chunk becomes
  compressible`. Some uncompressed chunk is always already compressible, so `days` was 0 in every
  receipt and the rule was `uncompressed_bytes > free − 100 GiB`.
- The uncompressed working set is already on the device. Comparing it with the free space asks for room
  for a second copy of it.
- On the days it fired, the device had 6.5 to 7.4 days of ingest left with compression stopped, at the
  measured arrival of 44.4 GiB/day (the issue's first figures, 7.7 to 8.6, used the receipts' own rate,
  which D1 shows to be too low).

Owner decision 2026-11-26: the critical is to say how long the PGDATA device can keep taking ingest.

## What changes

- **Rule.** Critical `PGDATA_INGEST_RUNWAY_BELOW_CRITICAL` when
  `working_set_free_bytes − safety_margin_bytes < runway_critical_days × daily_arrival_bytes`
  (margin 100 GiB as before, `runway_critical_days` 3, both overridable). The old code is no longer
  emitted.
- **Rate.** New receipt field `daily_arrival_bytes`: per governed hypertable the largest size-per-day
  among its uncompressed chunks, summed. New receipt field `ingest_runway_days`.
- **Fail closed.** A zero rate leaves `free < margin` as the comparison. A rate that is absent, negative
  or not finite after a successful catalog observation is `WORKING_SET_UNAVAILABLE`, not a skipped check.
- **Diagnostics.** The stderr lines the OnFailure mail quotes name the runway, the rate, the free bytes,
  the margin and the filesystem.
- **Docs and instructions.** The runbooks that describe the comparison, and the sentence of the root
  agent instructions about it.

## Decisions

- **D1. Why not the receipt's `daily_ingest_bytes` as the rate.** It is the trailing seven-day sum of
  current chunk relation sizes divided by 7. A compressed chunk's own relation is nearly empty (measured
  2026-11-26: the 28 compressed river chunks sum to 0.0 GiB by `pg_total_relation_size`; their data is
  in the compressed relations), so the mean is the uncompressed bytes of the window divided by 7:
  31.9 GiB/day on 2026-11-25 with two of seven days compressed, against 44.4 GiB/day of arrival (river
  41.8 + forcing 2.6). It depends on how far compression has got, which is the opposite of what a
  runway "if compression stops" needs. It is not used, not even as a floor: it can never exceed the
  new rate while chunks are day chunks (at most seven per hypertable in the window, each at most
  the largest uncompressed one or nearly empty).
- **D2. "Largest uncompressed chunk per hypertable, per day of its interval."** A day chunk's final
  uncompressed size is one day of arrival. The largest uncompressed chunk is a finished one in steady
  state (measured: four finished river chunks at 41.8 GiB, today's 37.7, the forecast-horizon days
  smaller), and the compression lag of two days guarantees that finished uncompressed chunks exist
  while ingest and compression both run. Taking the largest errs towards an earlier alarm. All
  uncompressed chunks are read, not only finished ones: simpler, never lower, and it covers a
  hypertable whose finished chunks are all compressed. Sizes are divided by the chunk's own interval.
  Only the canonical hypertables contribute: a `_legacy` sibling receives no ingest, and one of its
  old wide chunks (268 to 409 GB over three days) would add a phantom rate large enough to make the
  critical fire every day.
- **D3. The aggregation is done in Python over catalog rows, not in SQL.** The unit tests feed plain
  rows, so per-hypertable maximum, interval normalisation and summing are exercised without a database.
  The added statement is a bare projection of `timescaledb_information.chunks` with
  `pg_total_relation_size` for the uncompressed chunks of the governed hypertables (about thirty rows
  today), in the transaction and statement timeout of the existing query.
- **D4. Nothing can silence the comparison.** The old rule's growth term was structurally zero and
  nobody noticed for months. Zero rate: the margin is the floor (with the margin overridden to zero and
  a zero rate nothing can fire; the default is 100 GiB). A zero rate must come from rows that were
  read (none of them canonical), not from a row statement that silently matched nothing: uncompressed bytes above zero
  with no row at all is a fault (`WORKING_SET_UNAVAILABLE`), and so is a rate that is absent when the
  comparison is reached. The check is "at least one row", not an equality of byte sums: the two
  statements run at READ COMMITTED and a chunk can change between them.
- **D5. The comparison no longer waits for the watermark.** It needs free bytes and a rate, not a
  projection. With `watermark_unavailable` both criticals can be present.
- **D6. A new code, not a new meaning under the old name.** The mail an operator receives carries the
  code; `PROJECTED_PEAK_EXCEEDS…` for a runway would mislead. Nothing parses the code: outside archives
  it appears in the script, one test file, two runbooks and the spec, and the alert unit quotes journal
  lines without reading them. The name says what runs out (the PGDATA device) and of what (ingest).
- **D7. `projected_peak_bytes` stays in the receipt as evidence.** No recommendation reads it any more.
- **D8. Three days, no warning tier.** The owner's number. `WORKING_SET_ABOVE_WARNING` (uncompressed
  above 400 GiB) remains the early signal; a second runway threshold is not added.

## Accepted risks (stated for the owner)

- **Growth that is not timeseries ingest.** The device is the `/home` volume. It also holds the shared
  data tree, caches and pytest temporary files; `daily_arrival_bytes` does not see them. The only mailed
  guard against that growth is this critical through its 100 GiB margin; `HOME_FREE_BELOW_WARNING`
  (300 GiB) is a warning and mails nothing. As a side effect the old rule mailed at roughly 390 to 470
  GiB free; the new one mails at about 233 GiB (100 + 3 × 44.4). That is the relaxation the owner
  chose; the margin itself was the only nominal guard before as well.
- **Lead time.** The audit runs once a day, so the first mail comes with two to three days of ingest
  left before the device is down to the margin, and about 2.25 days more (100 / 44.4) to empty.
- **A step in the arrival rate is seen late.** New basins write into the forecast-horizon chunks first;
  the largest chunk reflects the new rate only when a day at that rate is finished, about a week
  later. Bytes already written are in the free space; only the rate lags. The same holds for the
  first week after a rebuild or a long outage, when only partly filled chunks exist and the rate reads
  low. The old rule did not see rate changes at all.

## Evidence plan

- **Replay.** The stored receipts of the cluster of 2026-11-05 through the new comparison with a
  constant 44.4 GiB/day (old receipts do not carry the new field; the constant is today's measured
  arrival and an upper bound for the earlier days of that cluster). Expected: the old rule fires 5
  times, the new one 0; smallest free space 388.7 GiB against a firing line of 233.2 GiB.
- **Synthetic stall.** From the 2026-11-25 receipt (426.5 GiB free): with compression stopped and
  44.4 GiB/day arriving, the day on which the new rule first fires and the days then left to the margin
  and to empty.
- **Live, read-only, by hand, away from 04:10Z.** On node-27 in a detached worktree: only the
  environment file of the governance unit is sourced (values never printed); the worktree's script is
  run directly, not the wrapper (the wrapper would run the production checkout, take the production
  lock and write into the directory the replay reads), with `--quiet` and `--summary-path` under
  `/home/nwm/tmp`. What the run touches: read-only sessions of the audit's database role on the
  production server (settings and sizes, the working-set statements, the display watermark), `du` over
  PGDATA and the object store, `systemctl` reads. Expected: `daily_arrival_bytes` about 44 GiB,
  `ingest_runway_days` about 7, no runway critical; compared with a direct catalog reading of the
  uncompressed chunk sizes taken in the same minute.
- After the merge: the next scheduled 04:10Z receipt.

## Not in this change

- The compression cadence, the lag, the per-tick bound (#3380).
- The two-day steps of the display watermark that make the uncompressed set alternate (#3379 comment).
- `WORKING_SET_ABOVE_WARNING`, `HOME_FREE_BELOW_WARNING` and the database-size info codes.
- The cold-receipt schema version and the historical shapes.
- `chunk_compression_stats.before_compression_total_bytes` as a rate source: it would not depend on
  what is still uncompressed, but whether the audit's role may read it and whether it counts as
  catalog-only is not established.

## Impact

- Spec: `timeseries-working-set-governance` (one requirement replaced, one modified; Purpose by hand
  after the archive).
- Code: `scripts/node27_resource_governance.py`, `packages/common/node27_resource_governance_collection.py`.
- Tests: `tests/test_node27_working_set.py`, `tests/test_node27_resource_governance.py`.
- Docs: `docs/runbooks/production-ops/artifacts-and-storage.md`,
  `docs/runbooks/tier-node27-timeseries-storage.md`, `docs/runbooks/node-27-bringup-checklist.md`,
  `instructions/agents/shared.md` with the generated `CLAUDE.md` / `AGENTS.md`.
