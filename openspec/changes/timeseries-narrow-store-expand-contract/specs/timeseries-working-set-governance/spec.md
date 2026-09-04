## ADDED Requirements

### Requirement: Resource governance SHALL measure the uncompressed working set and project the next compression peak

The node-27 resource-governance audit SHALL collect, from the catalog only: `uncompressed_bytes` (sum of `pg_total_relation_size` over uncompressed chunks of the canonical and `_legacy` river and forcing hypertables), `daily_ingest_bytes` (computed PER canonical governed hypertable and summed: for each canonical hypertable, the `before_compression_total_bytes` of its latest-by-`range_end` chunk with `compression_status = 'Compressed'` in `chunk_compression_stats('<schema>.<table>')` joined to `timescaledb_information.chunks`, divided by that chunk's own width `days(range_end − range_start)`; a `_legacy` sibling contributes zero and is never borrowed from; NO uncompressed chunk is ever read for the rate, because a `valid_time`-partitioned chunk receives most of its bytes before the watermark reaches it and any uncompressed-bytes-over-age reading over-reports 24× at the chunk boundary — the 2026-09-04 node-27 receipt at 71bc5265 reported 8.4 TB/day against ≈78 GB/day; a compressed chunk has received every write it will get), `ingest_reference` (per canonical hypertable the chunk, range, pre-compression bytes, width and per-table rate the divisor came from, or `null` when that table has no compressed chunk), `next_compressible_at` (the oldest uncompressed chunk's `range_end` plus the compression lag read from `NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS` in the governance lane's own env — the same variable name the compression runner uses, default 172800, template-to-template cross-pinned — and echoed in the receipt as `compression_lag_seconds` so the node-27 rollout receipt compares it with the deployed compression env value), `home_free_bytes`, `projection_status`, and `projected_peak_bytes = uncompressed_bytes + daily_ingest_bytes × max(0, days(next_compressible_at − display watermark))` where the interval is expressed in days and may be fractional.

#### Scenario: Fields present in the receipt
- **WHEN** the audit runs in any mode
- **THEN** the receipt validates against the updated schema and carries all six fields plus `ingest_reference`, byte units, the watermark used and `projection_status = "ok"`

#### Scenario: No uncompressed chunk
- **WHEN** every chunk of every governed hypertable is compressed
- **THEN** `next_compressible_at` is null, `projected_peak_bytes` equals `uncompressed_bytes` (zero), `projection_status = "no_uncompressed_chunk"`, and no critical recommendation is emitted

#### Scenario: Watermark unavailable
- **WHEN** the display watermark cannot be fetched
- **THEN** the audit records `projection_status = "watermark_unavailable"`, emits the critical recommendation `WATERMARK_UNAVAILABLE` (a lane fault must reach an operator), and exits 1

#### Scenario: The rate reads the last compressed chunk, not the working set
- **WHEN** the watermark is 2026-09-03T00:00Z, the only uncompressed river chunk spans 2026-09-03..09-10 with `range_start` equal to the watermark and 324 GB on disk, and the latest compressed river chunk spans 2026-08-27..09-03 with `before_compression_total_bytes = 548,636,811,264` (the live node-27 shape at 71bc5265)
- **THEN** the river table's rate is `548,636,811,264 / 7` bytes per day (≈78 GB/day, not the 8.4 TB/day an age divisor reports), `uncompressed_bytes` is 324 GB, `ingest_reference["hydro.river_timeseries"]` names that compressed chunk, its bytes and `width_days = 7`, and the projected peak stays below one terabyte

#### Scenario: Latest compressed chunk, not the largest
- **WHEN** a table has two compressed chunks and the older one carries more `before_compression_total_bytes`
- **THEN** the rate is derived from the one with the greater `range_end`

#### Scenario: Width comes from the catalog
- **WHEN** the reference chunk spans one day (the post-expand narrow table) or seven days (today's canonical table)
- **THEN** the divisor is that chunk's own `range_end − range_start` in days — one and seven respectively — never a constant

#### Scenario: Only a compressed row is a reference
- **WHEN** `chunk_compression_stats` also returns rows with `compression_status = 'Uncompressed'`
- **THEN** those rows are ignored for the rate even if newer than every compressed row

#### Scenario: A write-frozen legacy sibling contributes no ingest
- **WHEN** `hydro.river_timeseries_legacy` still holds compressed and uncompressed chunks after the expand
- **THEN** its uncompressed chunks count toward `uncompressed_bytes`, it contributes zero to `daily_ingest_bytes`, and it is never used as the canonical table's reference

#### Scenario: No compressed reference
- **WHEN** a canonical hypertable has uncompressed chunks but no compressed chunk (the narrow table during its first lag + width days after the expand; a fresh install)
- **THEN** the audit records `projection_status = "no_compressed_reference"`, `daily_ingest_bytes = null`, `ingest_reference[<table>] = null`, `projected_peak_bytes = uncompressed_bytes` (no growth claim), emits the WARNING `NO_COMPRESSED_REFERENCE` naming the table, never `PROJECTED_PEAK_EXCEEDS_HOME_FREE`, and exits 0 unless another critical fires

#### Scenario: Home free space unavailable
- **WHEN** the working set was measured but the `/home` filesystem observation is `unavailable`
- **THEN** `home_free_bytes` is null, `projection_status` is left unchanged (`"ok"` or `"no_uncompressed_chunk"` — whichever the measurement produced), the audit emits the critical recommendation `HOME_FREE_UNAVAILABLE` (the projection cannot be compared to anything — a lane fault must reach an operator) and exits 1

#### Scenario: Database unreachable
- **WHEN** the audit cannot open its PostgreSQL connection (`connection_failed`) or the driver is missing (`psycopg2_unavailable`)
- **THEN** the receipt's `postgres` block records the reason, the audit emits the critical recommendation `POSTGRES_UNAVAILABLE` and exits 1; only a missing `DATABASE_URL` (`database_url_missing`, the configured-not-to-look skip) stays a silent exit 0

#### Scenario: Working set unavailable
- **WHEN** the working-set collection fails (the timescale block is `blocked`) or the catalog returns no row for either canonical hypertable
- **THEN** the audit records `projection_status = "working_set_unavailable"`, emits the critical recommendation `WORKING_SET_UNAVAILABLE` (an empty catalog MUST be distinguishable from "every chunk is compressed"), and exits 1

#### Scenario: No row scan
- **WHEN** the collection SQL is inspected by the catalog-only guard test (new in I6)
- **THEN** none of the new queries reference a chunk or hypertable in a FROM clause other than `timescaledb_information.*`, `pg_*` size functions and `chunk_compression_stats(<hypertable literal>)` (a catalog function over compression statistics, not a scan of the hypertable it names)

### Requirement: Critical SHALL mean the projected peak does not fit; database size SHALL be informational

The audit SHALL emit `PROJECTED_PEAK_EXCEEDS_HOME_FREE` as critical when `projected_peak_bytes > home_free_bytes − safety_margin_bytes` (safety margin default 100 GiB, operator-overridable), and `WORKING_SET_ABOVE_WARNING` as warning when `uncompressed_bytes` exceeds a configurable threshold (default 400 GiB). `DATABASE_SIZE_ABOVE_WARNING` and `DATABASE_SIZE_ABOVE_CRITICAL` SHALL be reported at info severity only and MUST NOT contribute to the non-zero exit or the OnFailure alert; the existing rule that any critical recommendation exits non-zero (`timeseries-db-retention`) is unchanged. The lane's runbook SHALL state the expected-red condition — while `projected_peak_bytes > home_free_bytes − safety_margin_bytes` the daily tick exits non-zero on `PROJECTED_PEAK_EXCEEDS_HOME_FREE` (a true capacity hazard, not a false positive), as a condition with a dated worked example rather than a date-bounded promise — and how the operator acknowledges it; the safety margin MUST NOT be raised to silence it.

#### Scenario: Peak fits
- **WHEN** `uncompressed_bytes = 600 GiB`, `daily_ingest_bytes = 75 GiB`, two days to `next_compressible_at`, `home_free_bytes = 900 GiB`
- **THEN** the audit records `projected_peak_bytes = 750 GiB`, emits no critical recommendation and exits 0 even though database size exceeds 500 GiB

#### Scenario: Peak does not fit
- **WHEN** the same inputs with `home_free_bytes = 800 GiB`
- **THEN** the audit emits `PROJECTED_PEAK_EXCEEDS_HOME_FREE`, prints `RESOURCE_GOVERNANCE_CRITICAL:PROJECTED_PEAK_EXCEEDS_HOME_FREE` on stderr and exits 1

#### Scenario: Alert body names the numbers that matter
- **WHEN** the OnFailure mail is rendered for that critical
- **THEN** its body states `projected_peak_bytes`, `home_free_bytes`, `next_compressible_at` and the working-set bytes, and does not lead with database size
