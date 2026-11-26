## ADDED Requirements

### Requirement: Critical SHALL mean the ingest runway on the PGDATA device is too short; database size SHALL be informational

With valid target evidence and a successful catalog observation, the audit SHALL emit
`PGDATA_INGEST_RUNWAY_BELOW_CRITICAL` as critical when
`working_set_free_bytes − safety_margin_bytes < runway_critical_days × daily_arrival_bytes`
(default margin 100 GiB, default `runway_critical_days` 3, both operator-overridable); equality fits.
The comparison does not depend on the display watermark.

The receipt SHALL record `ingest_runway_days = (working_set_free_bytes − safety_margin_bytes) /
daily_arrival_bytes`. It may be negative. It is null when `daily_arrival_bytes` is zero, when the target
evidence is not valid, or when the catalog observation failed.

The alarm states how long the device that holds PGDATA can keep taking timeseries ingest if compression
stops today. It SHALL NOT compare the uncompressed working set, which is already on the device, with
the free space (#3379: that comparison asked for room for a second copy of the working set and fired
on days with 6.5 days or more of runway at the measured arrival of 44.4 GiB/day).

When `daily_arrival_bytes` is zero the comparison reduces to
`working_set_free_bytes < safety_margin_bytes`: with a positive margin, a rate that reads zero does not
silence the alarm on a device that is nearly full. When the catalog observation succeeded but
`daily_arrival_bytes` is absent, negative or not finite, the audit SHALL emit `WORKING_SET_UNAVAILABLE`
as critical instead of skipping the comparison.

`WORKING_SET_ABOVE_WARNING` remains a configurable uncompressed-byte warning (default 400 GiB).
`DATABASE_SIZE_ABOVE_WARNING` and `DATABASE_SIZE_ABOVE_CRITICAL` remain info and SHALL NOT cause nonzero
exit or OnFailure alerts. Any critical recommendation still exits nonzero.

#### Scenario: Six days of runway is not critical

- **WHEN** valid PGDATA `working_set_free_bytes = 388.7 GiB`, the margin is 100 GiB,
  `daily_arrival_bytes = 44.4 GiB`, `uncompressed_bytes = 373.5 GiB` (more than the 288.7 GiB the
  device has free above the margin), and an unrelated filesystem has insufficient headroom
- **THEN** the audit records `ingest_runway_days` of about 6.5, emits no critical recommendation and
  exits 0 even though database size exceeds 500 GiB

#### Scenario: Less than three days of runway is critical

- **WHEN** valid PGDATA `working_set_free_bytes = 230 GiB`, the margin is 100 GiB and
  `daily_arrival_bytes = 44.4 GiB` (runway about 2.9 days), while unrelated filesystems have abundant
  free space
- **THEN** the audit emits `PGDATA_INGEST_RUNWAY_BELOW_CRITICAL`, prints
  `RESOURCE_GOVERNANCE_CRITICAL:PGDATA_INGEST_RUNWAY_BELOW_CRITICAL` on stderr and exits 1

#### Scenario: Exactly three days fits

- **WHEN** `working_set_free_bytes − safety_margin_bytes` equals `3 × daily_arrival_bytes`
- **THEN** no runway critical is emitted

#### Scenario: A zero rate leaves the margin as the floor

- **WHEN** `daily_arrival_bytes` is zero and `working_set_free_bytes` is 90 GiB with the 100 GiB margin
- **THEN** the audit emits `PGDATA_INGEST_RUNWAY_BELOW_CRITICAL` and records `ingest_runway_days` null
- **AND** with 150 GiB free and the same zero rate it emits none

#### Scenario: A missing rate is a fault, not a pass

- **WHEN** the catalog observation reports success and the working set carries no `daily_arrival_bytes`,
  or a negative or non-finite one
- **THEN** the audit emits `WORKING_SET_UNAVAILABLE` as critical and exits 1

#### Scenario: The comparison does not wait for the watermark

- **WHEN** the display watermark cannot be fetched and the catalog observation succeeded
- **THEN** the runway comparison is still made, next to the `WATERMARK_UNAVAILABLE` critical

#### Scenario: Alert body names the numbers that matter

- **WHEN** the OnFailure mail is rendered for the runway critical
- **THEN** its body states `ingest_runway_days`, `daily_arrival_bytes`, `working_set_free_bytes`, the
  safety margin, the resolved PGDATA path/device and `uncompressed_bytes`, and does not lead with
  database size or leak credentials

#### Scenario: The retired code is not emitted

- **WHEN** the audit runs in any state
- **THEN** no recommendation carries the code `PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE`

## MODIFIED Requirements

### Requirement: Resource governance SHALL measure the uncompressed working set and project the next compression peak

The node-27 resource-governance audit SHALL collect from the catalog only: `uncompressed_bytes` (sum
of `pg_total_relation_size` over uncompressed canonical and `_legacy` river/forcing chunks),
`daily_ingest_bytes` (mean daily uncompressed growth over the trailing seven days using chunk
`range_start` and size, never row scans), `next_compressible_at` (oldest uncompressed range end plus
the compression runner's lag), `projection_status`, watermark and
`projected_peak_bytes = uncompressed_bytes + daily_ingest_bytes × max(0, days(next_compressible_at − display watermark))`.
The interval is fractional days. `projected_peak_bytes` is evidence only: no recommendation is derived
from it (see "Critical SHALL mean the ingest runway on the PGDATA device is too short; database size SHALL be informational").

The audit SHALL also collect `daily_arrival_bytes`, the uncompressed bytes that arrive per day: for each
canonical governed hypertable the largest `pg_total_relation_size / chunk interval in days` among its
uncompressed chunks, summed over those hypertables; zero when none of them has an uncompressed chunk. A
`_legacy` sibling receives no ingest: its chunks count in `uncompressed_bytes` and never in the rate. The
rate is read from catalog rows of the uncompressed chunks of all governed hypertables in the same
read-only transaction as the other working-set fields, and no working-set field is recorded unless
every statement of that transaction succeeded, the rows are consistent with the aggregate
(`uncompressed_bytes` above zero implies at least one row) and every row has a positive chunk interval.
`daily_ingest_bytes` is not that rate: a compressed chunk's own relation is nearly empty (its data lives
in the compressed relation), so the trailing mean is the uncompressed bytes of the last seven days
divided by seven, whatever arrived. Separately it SHALL observe the configured PGDATA filesystem
directly and report `working_set_free_bytes` and `working_set_filesystem` with resolved path, device
identity and status.

#### Scenario: Fields present in the receipt

- **WHEN** the audit runs in any mode
- **THEN** the current receipt validates with the catalog projection fields and destination-bound
  capacity fields; projection status is `ok` when catalog and watermark observations succeed,
  independently of capacity observation status

#### Scenario: No uncompressed chunk

- **WHEN** every chunk of every governed hypertable is compressed
- **THEN** `next_compressible_at` is null, `projected_peak_bytes` equals `uncompressed_bytes`
  (zero), `projection_status = "no_uncompressed_chunk"`, `daily_arrival_bytes` is zero, and the
  runway comparison is made like in any other state; missing required filesystem or usage evidence
  still fails closed

#### Scenario: Watermark unavailable

- **WHEN** the display watermark cannot be fetched
- **THEN** the audit records `projection_status = "watermark_unavailable"`, emits the critical
  recommendation `WATERMARK_UNAVAILABLE` (a lane fault must reach an operator), and exits 1

#### Scenario: No row scan

- **WHEN** the collection SQL is inspected by the existing catalog-only guard test
- **THEN** none of the new queries reference a chunk or hypertable in a FROM clause other than
  `timescaledb_information.*` and `pg_*` size functions

#### Scenario: Arrival is the largest uncompressed chunk of each hypertable

- **WHEN** the river hypertable's uncompressed day chunks measure 41.8 GiB (four that have ended),
  37.7 GiB (today's) and less (the forecast-horizon days), the forcing hypertable's largest measures
  2.6 GiB, and two of the last seven days are already compressed
- **THEN** the receipt records `daily_arrival_bytes` of 44.4 GiB, while `daily_ingest_bytes` is the
  smaller trailing mean (31.9 GiB)

#### Scenario: Arrival is per day whatever the chunk interval

- **WHEN** a governed hypertable's largest uncompressed chunk spans seven days and measures 70 GiB
- **THEN** that hypertable contributes 10 GiB to `daily_arrival_bytes`

#### Scenario: A hypertable with no uncompressed chunk contributes nothing

- **WHEN** every chunk of one governed hypertable is compressed and another has uncompressed chunks
- **THEN** `daily_arrival_bytes` is the other hypertable's contribution alone

#### Scenario: A legacy chunk does not enter the rate

- **WHEN** a `_legacy` sibling holds an uncompressed three-day chunk of 300 GiB next to the canonical
  hypertables of the first scenario
- **THEN** `daily_arrival_bytes` is still 44.4 GiB and `uncompressed_bytes` includes the 300 GiB

#### Scenario: Rows that contradict the aggregate are a fault

- **WHEN** the aggregate statement reports `uncompressed_bytes` above zero and the row statement returns
  no row, or a row has a null or non-positive chunk interval
- **THEN** the working set is recorded as `catalog_unavailable` with every catalog field null, and the
  audit emits `WORKING_SET_UNAVAILABLE`

#### Scenario: A failed arrival read leaves no partial working set

- **WHEN** the statement that reads the uncompressed chunks fails after the aggregate statement succeeded
- **THEN** the working set is recorded as `catalog_unavailable` with no `uncompressed_bytes`, and the
  audit emits `WORKING_SET_UNAVAILABLE`

## REMOVED Requirements

### Requirement: Critical SHALL mean the projected peak does not fit; database size SHALL be informational

**Reason**: The rule `projected_peak_bytes > working_set_free_bytes − safety_margin_bytes` compared bytes
that are already on the device with its free space. In all 45 stored receipts the growth term of the
projection was zero (some uncompressed chunk was always already compressible), so the rule was
`uncompressed_bytes > free − margin`: it fired five times in thirteen days with 6.5 to 7.4 days of
ingest runway left at the measured arrival of 44.4 GiB/day (#3379). Owner decision 2026-11-26.

**Migration**: Replaced by "Critical SHALL mean the ingest runway on the PGDATA device is too short; database size SHALL be informational". The code `PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE` is no longer
emitted; stored receipts that carry it stay readable. `projected_peak_bytes` stays in the receipt as
evidence.
