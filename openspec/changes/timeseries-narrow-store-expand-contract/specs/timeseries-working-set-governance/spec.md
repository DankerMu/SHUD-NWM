## ADDED Requirements

### Requirement: Resource governance SHALL measure the uncompressed working set and project the next compression peak

The node-27 resource-governance audit SHALL collect from the catalog only:
`uncompressed_bytes` (sum of `pg_total_relation_size` over uncompressed canonical and `_legacy`
river/forcing chunks), `daily_ingest_bytes` (mean daily uncompressed growth over the trailing seven
days using chunk `range_start` and size, never row scans), `next_compressible_at` (oldest uncompressed
range end plus the compression runner's lag), `projection_status`, watermark and
`projected_peak_bytes = uncompressed_bytes + daily_ingest_bytes ×
max(0, days(next_compressible_at − display watermark))`. The interval is fractional days.
Separately it SHALL observe the configured PGDATA filesystem directly and report
`working_set_free_bytes` and `working_set_filesystem` with resolved path, device identity and status.

#### Scenario: Fields present in the receipt
- **WHEN** the audit runs in any mode
- **THEN** the current receipt validates with the catalog projection fields and destination-bound capacity fields; projection status is `ok` when catalog and watermark observations succeed, independently of capacity observation status

#### Scenario: No uncompressed chunk
- **WHEN** every chunk of every governed hypertable is compressed
- **THEN** `next_compressible_at` is null, `projected_peak_bytes` equals `uncompressed_bytes` (zero), `projection_status = "no_uncompressed_chunk"`, and no peak critical is emitted; missing required filesystem or usage evidence still fails closed

#### Scenario: Watermark unavailable
- **WHEN** the display watermark cannot be fetched
- **THEN** the audit records `projection_status = "watermark_unavailable"`, emits the critical recommendation `WATERMARK_UNAVAILABLE` (a lane fault must reach an operator), and exits 1

#### Scenario: No row scan
- **WHEN** the collection SQL is inspected by the existing catalog-only guard test
- **THEN** none of the new queries reference a chunk or hypertable in a FROM clause other than `timescaledb_information.*` and `pg_*` size functions

### Requirement: Critical SHALL mean the projected peak does not fit; database size SHALL be informational

With valid target evidence, the audit SHALL emit `PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE` as
critical when `projected_peak_bytes > working_set_free_bytes − safety_margin_bytes`
(default margin 100 GiB, operator-overridable); equality fits. `WORKING_SET_ABOVE_WARNING` remains
a configurable uncompressed-byte warning (default 400 GiB). `DATABASE_SIZE_ABOVE_WARNING` and
`DATABASE_SIZE_ABOVE_CRITICAL` remain info and SHALL NOT cause nonzero exit or OnFailure alerts.
Any critical recommendation still exits nonzero.

#### Scenario: Peak fits
- **WHEN** `uncompressed_bytes = 600 GiB`, `daily_ingest_bytes = 75 GiB`, two days to `next_compressible_at`, valid PGDATA `working_set_free_bytes = 900 GiB`, and unrelated `/home` has insufficient headroom
- **THEN** the audit records `projected_peak_bytes = 750 GiB`, emits no critical recommendation and exits 0 even though database size exceeds 500 GiB

#### Scenario: Peak does not fit
- **WHEN** the same projection has valid PGDATA `working_set_free_bytes = 800 GiB` while unrelated `/home` has abundant free space
- **THEN** the audit emits `PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE`, prints `RESOURCE_GOVERNANCE_CRITICAL:PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE` on stderr and exits 1

#### Scenario: Alert body names the numbers that matter
- **WHEN** the OnFailure mail is rendered for that critical
- **THEN** its body states `projected_peak_bytes`, `working_set_free_bytes`, the resolved PGDATA path/device, `next_compressible_at` and working-set bytes, and does not lead with database size or leak credentials

### Requirement: Capacity SHALL be bound to the configured PGDATA filesystem without fallback

The observer SHALL reuse direct filesystem/device observations of configured PGDATA, not path-prefix
matching or selection among home/cold labels. Available capacity means `f_bavail` bytes. Missing or
conflicting target evidence SHALL emit critical `WORKING_SET_FILESYSTEM_UNAVAILABLE` with an honest
unavailable/ambiguous reason and exit 1, regardless of projection state. Existing PGDATA du observation
failure SHALL remain visible and emit independent `PGDATA_USAGE_UNAVAILABLE`; this change SHALL NOT
add a duplicate recursive scan. Historical/home telemetry is never a current capacity fallback.

#### Scenario: Aliases on the same device
- **WHEN** PGDATA uses the old default or a resolved alias on the same device as several home/repo/object-store observation labels
- **THEN** its direct path/device observation supplies capacity without false ambiguity or double charging a retained old directory

#### Scenario: Missing target with an empty working set
- **WHEN** all chunks are compressed but target statvfs/path/device observation is unavailable
- **THEN** capacity remains unavailable and the audit emits `WORKING_SET_FILESYSTEM_UNAVAILABLE` and exits 1, not a healthy skipped comparison

#### Scenario: Conflicting target or missing usage evidence
- **WHEN** target filesystem identity conflicts with the existing observation of that same PGDATA target, or its existing du observation is unavailable
- **THEN** identity conflict is explicitly ambiguous and critical; missing usage independently emits `PGDATA_USAGE_UNAVAILABLE`; neither path borrows home free bytes

### Requirement: Historical receipt meaning SHALL remain stable while current writers cut over

Cold receipt schema version 1.0 and history reading SHALL remain compatible. The optional
working-set object SHALL admit separate closed historical-home and current-destination shapes.
Current producers SHALL emit only the destination shape; malformed hybrid shapes SHALL be rejected.
No-working-set historical receipts remain valid. Accepting historical evidence does not certify
current PGDATA capacity or authorize a legacy-field fallback in the current comparator.

#### Scenario: Read historical and emit current evidence
- **WHEN** an old receipt has no working set or the historical `home_free_bytes` shape and a new CLI audit emits its optional cold receipt
- **THEN** history remains readable, the new receipt validates with destination fields and no obsolete current-writer alias, and both receipt/error surfaces remain DSN-safe
