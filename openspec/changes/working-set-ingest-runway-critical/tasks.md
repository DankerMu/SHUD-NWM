# Tasks: the working-set critical becomes an ingest runway (#3379)

Fixture level: `standard`. Risk packs: production alerting semantics (a critical is relaxed), catalog-only
SQL on the production database, receipt shape.

## Must preserve

- The audit stays read-only and catalog-only: every collection statement reads only
  `timescaledb_information.*` and `pg_*` size functions, never a chunk or hypertable. The guard test
  is extended to check every statement of the working-set collection, not only the one it finds today.
- Capacity is the direct observation of the configured PGDATA filesystem; no fallback to home telemetry.
- `WORKING_SET_FILESYSTEM_UNAVAILABLE`, `PGDATA_USAGE_UNAVAILABLE`, `WATERMARK_UNAVAILABLE`,
  `WORKING_SET_ABOVE_WARNING` and the database-size info codes: conditions, severities and exit codes
  unchanged. `WORKING_SET_UNAVAILABLE` keeps its present condition and gains the missing-rate one.
- The fields the receipt has today keep their names, meanings and values (`projected_peak_bytes` and
  `daily_ingest_bytes` included); the existing aggregate statement is not rewritten. Historical
  receipts stay readable.
- stderr diagnostics stay free of credentials; `--quiet` still prints the critical lines.

## 1. Fixture

- [x] 1.1 Fixture review (one read-only reviewer, at most two revise rounds);
      `openspec validate working-set-ingest-runway-critical --strict --no-interactive` passes.

## 2. Collection

- [ ] 2.1 A second statement in the same read-only transaction and statement timeout returns one row
      per uncompressed chunk of the governed hypertables (hypertable, range start, range end,
      `pg_total_relation_size`). A pure function turns such rows into `daily_arrival_bytes`: per
      canonical hypertable the largest `bytes / interval days`, summed; the canonical ones are
      `CANONICAL_HYPERTABLES` of `node27_timeseries_discovery`, `LEGACY_HYPERTABLES` contribute nothing; zero for no canonical row; a row whose
      interval is null or not positive is a fault.
- [ ] 2.2 No working-set field is assigned before both statements have succeeded: a failure of either
      leaves `catalog_unavailable` with every catalog field null, and so does `uncompressed_bytes`
      above zero with no row, or a faulty row. The early return of the "no uncompressed chunk" state
      carries `daily_arrival_bytes` zero.
- [ ] 2.3 The receipt's working-set object carries `daily_arrival_bytes` and `ingest_runway_days`; every
      place that enumerates or validates that object's keys follows (current-destination shape only;
      the historical shapes are untouched).

## 3. Rule and diagnostics

- [ ] 3.1 `PGDATA_INGEST_RUNWAY_BELOW_CRITICAL` replaces `PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE` as
      specified: strict `<`, zero rate reduces to the margin, evaluated for every successful catalog
      status (`ok`, `no_uncompressed_chunk`, `watermark_unavailable`) with valid capacity.
- [ ] 3.2 With a successful catalog status, a rate that is absent, negative, not finite or not a number
      emits `WORKING_SET_UNAVAILABLE` (critical). The comparison is never skipped silently.
- [ ] 3.3 `runway_critical_days` is a threshold with default 3 and the same override mechanism as
      `safety_margin_bytes` (CLI option; an env key only if the other thresholds have one). A
      non-positive or non-finite value is refused like the other invalid thresholds.
- [ ] 3.4 The stderr diagnostic of the runway critical names `ingest_runway_days`, `daily_arrival_bytes`,
      `working_set_free_bytes`, `safety_margin_bytes`, `uncompressed_bytes` and the filesystem object;
      the two filesystem criticals keep a diagnostic that fits them.

## 4. Tests

- [ ] 4.1 One test per scenario of the two requirements, with the scenario's numbers. Tests of the old
      rule are rewritten to the new one, not deleted; each rewritten test says what it proves now. The
      working-set mocks that carry no rate are given one, except the one that becomes the missing-rate
      test: those of `tests/test_node27_resource_governance.py`, and
      `test_current_comparator_never_falls_back_to_historical_home` of `tests/test_node27_working_set.py`
      (status `ok`, no rate, expects exactly `WORKING_SET_FILESYSTEM_UNAVAILABLE`: with a rate it still
      does). The fake cursor's default for the row statement must be consistent with its aggregate row.
      The missing-rate critical's action text names the cause.
- [ ] 4.2 The pure rate function on plain rows: per-hypertable maximum (not the sum, not the global
      maximum), interval normalisation, summing over hypertables, a hypertable with no row, no rows at
      all, a `_legacy` row, a null and a non-positive interval.
- [ ] 4.3 Boundary of the rule: `free − margin` one byte below, equal to, and one byte above `3 × rate`.
- [ ] 4.4 Red runs or mutations, each restored: comparison flipped to the old rule (the "six days of
      runway" case goes red); global maximum instead of per-hypertable; sum instead of maximum; the
      interval normalisation removed; the zero-rate floor removed; the missing-rate critical removed;
      the old code emitted again; a field assigned before the second statement ran; the `_legacy`
      exclusion removed; the rows-against-aggregate check removed.
- [ ] 4.5 A test that no recommendation is derived from `projected_peak_bytes` (any value, same
      recommendations).

## 5. Docs (orchestrator)

- [ ] 5.1 `docs/runbooks/production-ops/artifacts-and-storage.md`,
      `docs/runbooks/tier-node27-timeseries-storage.md`, `docs/runbooks/node-27-bringup-checklist.md`: the
      comparison, the code, the numbers an operator reads in the mail, what to do (free space or restore
      compression), and that growth other than ingest is covered only by the margin.
- [ ] 5.2 `instructions/agents/shared.md`: the sentence about the working-set peak says what is compared
      now; `CLAUDE.md` and `AGENTS.md` regenerated byte-exact.

## 6. Evidence Floor

- [ ] 6.1 Touched test files pass locally; `ruff check --no-cache .` clean; entropy hard gate unchanged
      (247 findings / 0 failing).
- [ ] 6.2 node-27, detached worktree, no database: the touched test files.
- [ ] 6.3 Replay and synthetic stall as in the proposal's evidence plan; the tables go into the PR.
- [ ] 6.4 Live read-only audit on node-27 as in the proposal's evidence plan, with the direct catalog
      reading next to it.
- [ ] 6.5 Code review (two seats: alert correctness and production SQL; test evidence); CI green.
