## ADDED Requirements

### Requirement: Resource governance SHALL measure the uncompressed working set and project the next compression peak

The node-27 resource-governance audit SHALL collect, from the catalog only: `uncompressed_bytes` (sum of `pg_total_relation_size` over uncompressed chunks of the canonical and `_legacy` river and forcing hypertables), `daily_ingest_bytes` (uncompressed chunk bytes whose `range_start` falls within the trailing seven days, divided by the number of days those chunks actually cover — `min(7, max(1, days(watermark − earliest in-window range_start)))`, never a fixed seven, because in steady state the uncompressed window holds only lag+1 days and a fixed divisor under-reports the rate; derived from chunk `range_start` and size, never from row scans), `next_compressible_at` (the oldest uncompressed chunk's `range_end` plus the compression lag read from `NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS` in the governance lane's own env — the same variable name the compression runner uses, default 172800, template-to-template cross-pinned — and echoed in the receipt as `compression_lag_seconds` so the node-27 rollout receipt compares it with the deployed compression env value), `home_free_bytes`, `projection_status`, and `projected_peak_bytes = uncompressed_bytes + daily_ingest_bytes × max(0, days(next_compressible_at − display watermark))` where the interval is expressed in days and may be fractional.

#### Scenario: Fields present in the receipt
- **WHEN** the audit runs in any mode
- **THEN** the receipt validates against the updated schema and carries all six fields, byte units, the watermark used and `projection_status = "ok"`

#### Scenario: No uncompressed chunk
- **WHEN** every chunk of every governed hypertable is compressed
- **THEN** `next_compressible_at` is null, `projected_peak_bytes` equals `uncompressed_bytes` (zero), `projection_status = "no_uncompressed_chunk"`, and no critical recommendation is emitted

#### Scenario: Watermark unavailable
- **WHEN** the display watermark cannot be fetched
- **THEN** the audit records `projection_status = "watermark_unavailable"`, emits the critical recommendation `WATERMARK_UNAVAILABLE` (a lane fault must reach an operator), and exits 1

#### Scenario: Working set unavailable
- **WHEN** the working-set collection fails (the timescale block is `blocked`) or the catalog returns no row for either canonical hypertable
- **THEN** the audit records `projection_status = "working_set_unavailable"`, emits the critical recommendation `WORKING_SET_UNAVAILABLE` (an empty catalog MUST be distinguishable from "every chunk is compressed"), and exits 1

#### Scenario: No row scan
- **WHEN** the collection SQL is inspected by the catalog-only guard test (new in I6)
- **THEN** none of the new queries reference a chunk or hypertable in a FROM clause other than `timescaledb_information.*` and `pg_*` size functions

### Requirement: Critical SHALL mean the projected peak does not fit; database size SHALL be informational

The audit SHALL emit `PROJECTED_PEAK_EXCEEDS_HOME_FREE` as critical when `projected_peak_bytes > home_free_bytes − safety_margin_bytes` (safety margin default 100 GiB, operator-overridable), and `WORKING_SET_ABOVE_WARNING` as warning when `uncompressed_bytes` exceeds a configurable threshold (default 400 GiB). `DATABASE_SIZE_ABOVE_WARNING` and `DATABASE_SIZE_ABOVE_CRITICAL` SHALL be reported at info severity only and MUST NOT contribute to the non-zero exit or the OnFailure alert; the existing rule that any critical recommendation exits non-zero (`timeseries-db-retention`) is unchanged. The lane's runbook SHALL state that the first deployment is expected to exit non-zero on `PROJECTED_PEAK_EXCEEDS_HOME_FREE` until the one-day-chunk expand lands (a true capacity hazard, not a false positive) and how the operator acknowledges it; the safety margin MUST NOT be raised to silence it.

#### Scenario: Peak fits
- **WHEN** `uncompressed_bytes = 600 GiB`, `daily_ingest_bytes = 75 GiB`, two days to `next_compressible_at`, `home_free_bytes = 900 GiB`
- **THEN** the audit records `projected_peak_bytes = 750 GiB`, emits no critical recommendation and exits 0 even though database size exceeds 500 GiB

#### Scenario: Peak does not fit
- **WHEN** the same inputs with `home_free_bytes = 800 GiB`
- **THEN** the audit emits `PROJECTED_PEAK_EXCEEDS_HOME_FREE`, prints `RESOURCE_GOVERNANCE_CRITICAL:PROJECTED_PEAK_EXCEEDS_HOME_FREE` on stderr and exits 1

#### Scenario: Alert body names the numbers that matter
- **WHEN** the OnFailure mail is rendered for that critical
- **THEN** its body states `projected_peak_bytes`, `home_free_bytes`, `next_compressible_at` and the working-set bytes, and does not lead with database size
