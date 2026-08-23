# Tasks

## 1. Implementation

- [x] T1 **Revert the withdrawn rebuild.** `packages/common/forecast_store.py`
      back to `ms.station_id ILIKE %s ESCAPE '\'` with the single shared pattern
      (`git diff origin/master -- packages/common/forecast_store.py` must be
      empty), and `tests/test_list_search_contract.py` back to its master state
      **except** the hardening of `test_met_station_search_escapes_wildcards`,
      which pins escape-order facts that are true either way — keep that, and say
      so in the receipt so the non-empty diff is not mistaken for a leftover.
- [x] T2 `db/migrations/000054_met_station_trgm_expression_index.sql` rewritten as
      three statements: `DROP INDEX CONCURRENTLY IF EXISTS
      met.met_station_id_trgm_idx`, and the same for `..._invalid` and
      `..._legacy`. No transaction wrapper, no `CREATE`, no `DO` block. Header
      carries the measurement that decided it, the original index definition from
      `000031:12-14` verbatim so a rebuild needs no archaeology, and the reason
      `CONCURRENTLY` is mandatory.
- [x] T3 `docs/adr/0004-identifier-trgm-gin-equality-trap.md`: record the
      reversal and the general lesson — check for a consumer before applying the
      expression-index convention, because the convention preserves a *useful*
      index while removing the trap and is the wrong tool when the index is not
      useful.
- [x] T4 `tests/test_real_database_integration.py`: remove
      `met_station_id_trgm_idx` from the expected index-name set at `:131-136`,
      and rework the test added for the withdrawn design so it asserts the index
      is **absent** — from `pg_indexes`, and from the equality plan under the
      default planner and with seqscan/indexscan/indexonlyscan disabled. Also
      assert `..._invalid` and `..._legacy` are absent.
- [x] T5 `tests/test_migrations.py`: rewrite the `000054` static tests for the
      drop shape — exactly three executable statements, every one a
      `DROP INDEX CONCURRENTLY IF EXISTS`, no `CREATE INDEX`, no `BEGIN;`/`COMMIT;`,
      and `met_station_name_trgm_idx` named in no executable statement.

## 2. Evidence Floor

- [x] E1 `uv run ruff check .` clean; `openspec validate
      met-station-trgm-expression-index --strict --no-interactive` valid; the
      unit suites green.
- [x] E2 node-27: migration applied, then applied a **second** time with no
      error. Receipt is the psql transcript.
- [x] E3 node-27 `EXPLAIN (ANALYZE, BUFFERS)` of the probe shape after the drop,
      under the default planner **and** with bitmap-only settings, showing
      `met_station_id_trgm_idx` absent from both plans. The before-half is already
      captured (§3) and cannot be retaken.
- [x] E4 node-27 hit-set equivalence: the same keyword set through the search
      path before and after, identical station sets and `total_count`. Keywords
      must include mixed case, `%`, `_`, `\`, and non-ASCII. The query text does
      not change, so this is a check that the *access path* change is invisible —
      state that framing rather than implying the SQL was rewritten.
- [x] E5 node-27 catalog: `met_station_id_trgm_idx`, `..._invalid` and
      `..._legacy` all absent from `pg_indexes`; `met_station_name_trgm_idx` still
      present and untouched.
- [x] E6 node-27 lock discipline: no ACCESS EXCLUSIVE on `met.met_station` while
      the migration runs. `pg_locks` sampling, or the statement list plus each
      statement's concurrency property — state which was used.
- [x] E7 node-27 real-DB pytest: `tests/test_real_database_integration.py` green
      under `NHMS_RUN_INTEGRATION=1`.
- [x] E8 `git diff --stat origin/master...HEAD` — the non-`openspec/`,
      non-`.workplans/` paths are exactly
      `db/migrations/000054_met_station_trgm_expression_index.sql`,
      `docs/adr/0004-identifier-trgm-gin-equality-trap.md`,
      `tests/test_real_database_integration.py`,
      `tests/test_list_search_contract.py` and `tests/test_migrations.py`.
      `packages/common/forecast_store.py` must **not** appear. Named rather than
      counted: a prior PR in this repository shipped a receipt of exactly this
      shape that was false.
- [x] E9 Quote the corrected ADR paragraph verbatim.
- [x] E10 **Assertion-count receipt for D3.** Show the diff of
      `tests/test_real_database_integration.py` and demonstrate that removing
      `met_station_id_trgm_idx` from the must-exist set is paid for by strictly
      more assertions, not fewer. Removing an oracle entry is the one edit here
      that has to be argued rather than asserted.

## 3. Receipts

### Pre-migration node-27 baseline (E3's "before" half)

Taken 2026-08-23 before any change, because it cannot be taken afterwards.
Full transcript `.workplans/issue-1669/before-20260823.txt` (PG 15.2 /
TimescaleDB 2.10.2 / pg_trgm 1.6, production `nhms`):

- default planner, `WHERE ms.active_flag = true AND ms.station_id = t.station_id`
  over a 500-row batch -> `Bitmap Index Scan on met_station_id_trgm_idx`,
  **168.60 ms**, shared hit 30,128 with 29,627 of them inside the trigram scan.
- the same with seqscan/indexscan/indexonlyscan disabled -> still that index,
  159.69 ms.
- `station_id ILIKE '%qhh%'` -> uses the index, 45.6 ms, **386 rows**.
- `lower(station_id) LIKE '%qhh%'` -> cannot use the index, falls back to
  `met_station_active_basin_station_idx` + Filter, 7.3 ms, **386 rows**.

Two things this settles. The trap needed no statistics flip to reproduce — it was
the default plan on first contact, which supersedes the issue's transcribed
2026-08-21 figures. And the index-free path was **six times faster** on the same
386 rows, which is the first hint that this index was not earning its keep.

### The index-usage measurement that reversed the approach

`stats_reset` is NULL, so these cover the cluster's entire life:

```
met_station_pkey                      idx_scan 101,500,503   2104 kB
met_station_id_trgm_idx               idx_scan       2,502   1664 kB
met_station_basin_idx                 idx_scan          57   1472 kB
met_station_active_basin_station_idx  idx_scan           8    784 kB
met_station_geom_gix                  idx_scan           0     18 MB
met_station_name_trgm_idx             idx_scan           0   2576 kB
```

`met_station_id_trgm_idx` stood at 500 before this issue was touched — PR #1666's
E4 probe count exactly — and the 2,502 is this change's own 500-iteration
baseline loops. `met_station_name_trgm_idx`, which shares the search `OR` and
would be lit by the same `BitmapOr`, has never been scanned. See D1.

### Withdrawn implementation (kept for the record)

The rebuild-as-expression-index version was implemented in full and verified
before it was withdrawn: migration `000054` shaped on `000052`, the
`lower(ms.station_id) LIKE` search arm, five mutation red receipts. It is not in
the final diff. One finding from it is worth keeping regardless of approach:

**A red receipt can be a false green through stale bytecode.** The mutation
`[a.lower(), b]` -> `[a, b.lower()]` is the same length, and the rewrite and
restore landed in the same wall-clock second, so CPython's mtime+size `.pyc`
validity check reused pre-mutation bytecode — the mutated source was on disk and
a live call still returned the unmutated tuple. Clearing `__pycache__` produced
the red. **Any red-receipt procedure in this repository that edits Python in
place can certify a false green** unless it clears the cache or perturbs mtime.

### Round-1 fix, and a measurement that settles the P2

Round 1 (two lenses, findings in `.workplans/pr-1771/review/round-1-findings.md`)
returned one P2: the `BitmapOr` corroboration was asserted as fact in three
places and is not proven, because
`met_station_active_basin_station_idx (basin_version_id, station_id) WHERE
active_flag = true` could serve the whole search predicate without touching
either GIN. Corrected in the migration header, ADR 0004, design.md D1 and
proposal.md; the implementer additionally pinned the correction with an assertion
(`tests/test_migrations.py:1307-1313` requires the header to keep naming
`000033_station_mvt_active_source_index.sql`), with a red receipt.

**Then it was measured, while the index still existed.** Probe k7 in
`.workplans/issue-1669/hitset-before-20260823.txt` runs the `else`-branch shape
exactly as `forecast_store` emits it — `basin_version_id = $0 AND active_flag =
true AND (station_id ILIKE ... OR COALESCE(station_name,'') ILIKE ...)`:

```
Index Scan using met_station_active_basin_station_idx on met_station ms
  Index Cond: (basin_version_id = $0)
  Filter: ((station_id ~~* '%q%') OR (COALESCE(station_name, '') ~~* '%q%'))
  Rows Removed by Filter: 1709   Buffers: shared hit=583
Execution Time: 3.783 ms
```

Neither GIN appears. The reviewer's alternative access path is not hypothetical —
it is what the planner actually chooses for the real search shape, with both
trigram indexes present and eligible. Two consequences:

1. The P2 was correct and the correction is not a hedge; the inference really
   does fail.
2. It **strengthens** the drop. The zero-scan reading was ambiguous, but this is
   not: the production search shape does not use `met_station_id_trgm_idx` even
   when it exists. The earlier isolated probes, which omitted `basin_version_id`,
   were the ones that made the GIN look load-bearing.

### E4 hit-set baseline, captured before the drop

`.workplans/issue-1669/hitset-before-20260823.txt`, fingerprints are
`md5(string_agg(station_id, ',' ORDER BY station_id))` over the search predicate
with `active_flag = true`:

| keyword | n | fingerprint |
|---|---|---|
| `QHH` (mixed case) | 386 | `50cb1aa35beaa74aefbbe00cdc2df2d1` |
| `qhh` (lower) | 386 | `50cb1aa35beaa74aefbbe00cdc2df2d1` |
| escaped `%` | 0 | — |
| escaped `_` | 6290 | `d2766ca3cdcaad6798805195ef9c1cca` |
| escaped `\` | 0 | — |
| `青海` (non-ASCII) | 0 | — |
| k7, full else-branch shape | 0 | — |

`QHH` and `qhh` agreeing on both count and fingerprint is the case-insensitivity
check. Three keywords return zero rows, which pins less than a non-empty result
would; recorded as a limit rather than presented as coverage.

### node-27 receipts, head `58333aee` (E2-E7)

Detached worktree `/home/nwm/NWM-1669`. Raw output in
`.workplans/issue-1669/{after,hitset-after}-20260823.txt` (gitignored, so the
substance is transcribed here).

**E2 — applied, and re-runnable in both senses.**
`uv run python -m packages.common.migrate` -> `2 applied, 46 skipped, 48 total`.
Second run of the runner -> `0 applied, 48 skipped`, i.e. the `schema_migrations`
guard holds. That only proves the *runner* is idempotent, so the SQL file was also
executed directly a second time, which is the stronger claim: it succeeded, with
`NOTICE: index "met_station_id_trgm_idx_invalid" does not exist, skipping` and the
same for `..._legacy`, which is exactly the `IF EXISTS` behaviour D2 relies on.

**Disclosed side effect**: the runner applies *all* pending migrations, and node-27
was one behind, so `000053_state_snapshot_clone_gate_kind.sql` was applied in the
same run. It is already merged on master and was simply not yet deployed; nothing
about it belongs to this change, and it is recorded here rather than left for
someone to find in the migration table.

**E3 — the trap is gone, and the table got faster.**

| probe | before | after |
|---|---|---|
| equality shape, default planner | Bitmap Index Scan on `met_station_id_trgm_idx`, **168.60 ms**, 30,128 buffers | `met_station_pkey`, **4.43 ms**, 2,000 buffers |
| equality shape, seq/index/index-only disabled | still the trigram index, 159.69 ms | Bitmap Index Scan on `met_station_pkey`, **2.25 ms** |
| `station_id ILIKE '%qhh%'` | trigram GIN, 45.6 ms, 386 rows | Index Only Scan on `met_station_active_basin_station_idx` + Filter, **11.37 ms**, **386 rows** |

38x on the equality shape, and the search arm this index nominally existed to
serve is 4x *faster* without it.

**E4 — hit sets identical.** All seven probes from
`hitset-before-20260823.txt` re-run after the drop: every count and every
`md5(string_agg(station_id, ',' ORDER BY station_id))` fingerprint matches,
including the mixed-case pair (`QHH` / `qhh`, 386 rows,
`50cb1aa35beaa74aefbbe00cdc2df2d1`) and the escaped-underscore probe (6,290 rows,
`d2766ca3cdcaad6798805195ef9c1cca`). Limit restated: three of the seven keywords
return zero rows on this data, so they pin less than a non-empty result would.

**E5 — catalog.** `met_station_id_trgm_idx`, `..._invalid` and `..._legacy` all
absent (`count = 0`). Five indexes remain, and `met_station_name_trgm_idx` is
still present and still bare
(`gin (COALESCE(station_name, ''::text) gin_trgm_ops) WHERE (active_flag = true)`),
i.e. untouched as the non-goal required.

**E6 — lock discipline.** A sampler polled `pg_locks` for
`AccessExclusiveLock` on `met.met_station` throughout the run and recorded
**zero** samples. Stated with its limit: each sample is a `docker exec psql`
round-trip, so the cadence is coarser than the nominal 0.2 s and a very short lock
could in principle fall between samples. The structural argument is what carries
this item — every statement is `DROP INDEX CONCURRENTLY`, which by definition does
not take ACCESS EXCLUSIVE on the table — and the sampling is corroboration.

**E7 — real-DB pytest.** `NHMS_RUN_INTEGRATION=1 uv run pytest -q
tests/test_real_database_integration.py` -> **11 passed in 15.19s** at
`58333aee`, including `test_met_station_trigram_index_is_dropped_and_unreachable`.

**E10 — assertion count.** `grep -cE '^[[:space:]]*assert |raise AssertionError'`
on `tests/test_real_database_integration.py`: master **93**, branch **103**. The
oracle reviewer re-derived both numbers independently rather than taking the
claim.

### Out-of-scope findings (reported, not fixed)

- `met_station_geom_gix` (18 MB) and `met_station_name_trgm_idx` (2,576 kB) have
  never been scanned. Both outside this issue's boundary.
- Cluster-wide: 209 indexes totalling 160 GB, of which 83 have never been
  scanned — though those 83 total only 131 MB, so the 160 GB is overwhelmingly in
  indexes that *are* used. "Index explosion" is real on this table and not the
  main story cluster-wide.
- `tests/test_migrations.py:13` — `EXPECTED_MIGRATIONS` is derived from the same
  glob that `test_all_migration_files_exist_with_expected_names` asserts against
  at `:66`. The test is tautological and cannot fail.
- `core.river_segment`'s trigram index, rebuilt as an expression index by the
  merged and deployed #1468, has `idx_scan` 4. The same question applies to it.
