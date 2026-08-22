# object-store-station-series-read Specification

## ADDED Requirements

### Requirement: The station CSV read SHALL absorb a concurrent atomic replacement with a bounded retry, and SHALL keep failing closed when it cannot

The producer that writes a station's forcing CSV replaces it atomically, which swaps the target's inode. The reader opens that file through a no-follow open whose mid-open identity comparison refuses exactly that swap. The two are the same shared artifact reached from two hosts, so the refusal is an expected consequence of ordinary production writes, not evidence of tampering or of a damaged file, and it SHALL no longer surface on the public API as a server error indistinguishable from a genuinely corrupt file. The reader SHALL re-attempt the open a bounded number of times when, and only when, the refusal carries the discriminator meaning the target's inode changed mid-open. The retry SHALL select on that discriminator field and SHALL NOT select on the refusal's message text, because the message is human-facing prose that may be reworded without notice while the discriminator is the contract. Every other refusal the open can raise — a symlinked or non-regular target, a containment violation, an I/O failure — SHALL propagate on the first attempt, since none of them is resolved by trying again and retrying them would delay a refusal the caller must see immediately.

The retry SHALL cover the open alone and SHALL NOT cover parsing the opened file. Once the descriptor exists it is bound to the pre-replacement inode, whose contents are a complete and self-consistent snapshot; any failure to parse what that descriptor yields is therefore a genuine content defect and SHALL surface on its first occurrence rather than being retried. Retrying the parse would both multiply the latency of a deterministic failure and hide defective producer output behind an apparent race.

When the bounded attempts are exhausted, the reader SHALL still fail closed with the same HTTP status and the same error code it returns for a malformed file, introducing no new status code and no new error type. Its reported reason SHALL nonetheless identify the concurrent replacement distinctly from a content defect, and that reason SHALL be derived from the discriminator rather than from the refusal's message text. Reasons reported for every other failure SHALL keep the operator-useful text they carry today.

The path where the file is absent SHALL remain untouched: an atomic replacement leaves no window in which the target path resolves to nothing, so a missing file is unrelated to this race and SHALL keep mapping to its existing not-found outcome without any retry.

#### Scenario: A replacement inside the open window is absorbed and the read succeeds

- **WHEN** the mid-open identity refusal is raised on an attempt, and a subsequent attempt within the bound opens the file successfully
- **THEN** the reader returns the parsed CSV content normally, and no error reaches the caller

#### Scenario: A persistent replacement exhausts the bound and fails closed

- **WHEN** every attempt within the bound raises the mid-open identity refusal
- **THEN** the reader raises the same malformed-file error, with the same HTTP status and error code as before this change
- **AND** the reported reason identifies the failure as a concurrent replacement, distinguishably from a content defect

#### Scenario: Refusals other than the identity change are not retried

- **WHEN** the open is refused for a symlinked target, a non-regular target, a containment violation, or an I/O failure
- **THEN** the reader attempts the open exactly once and surfaces the refusal immediately
- **AND** the reported reason keeps the operator-useful text it carried before this change

#### Scenario: The retry decision does not depend on message text

- **WHEN** a refusal carries the mid-open identity discriminator but wording unlike the primitive's current message, or carries another discriminator but wording resembling it
- **THEN** the retry decision follows the discriminator in both cases

#### Scenario: A content defect in the opened file is not retried

- **WHEN** the open succeeds and parsing the resulting descriptor fails because the file is empty, its header is unreadable, a declared data row is blank, or a declared bound is exceeded
- **THEN** the reader raises the malformed-file error on that first parse failure without re-opening the file

#### Scenario: A missing file keeps its existing outcome

- **WHEN** the expected station CSV path does not exist
- **THEN** the reader raises its existing not-found error with no retry

## MODIFIED Requirements

### Requirement: CSV parse and valid_time computation

The reader SHALL parse the per-station shud CSV with the documented two-row header (`nrow ncol start_date end_date` then `Time_Day Precip Temp RH Wind RN`) and emit (variable, valid_time, value) tuples where `valid_time = cycle_time + timedelta(seconds=int(round(Time_Day*86400)))`.

#### Scenario: valid_time of first data row equals cycle_time

- **WHEN** the reader parses a shud CSV whose first data row has `Time_Day=0`
- **AND** the cycle_time is `2026-06-20T12:00:00Z`
- **THEN** the emitted `valid_time` of that row SHALL be `2026-06-20T12:00:00Z`

#### Scenario: 3-hour step at Time_Day=0.125

- **WHEN** the second data row has `Time_Day=0.125`
- **AND** cycle_time is `2026-06-20T12:00:00Z`
- **THEN** the emitted `valid_time` SHALL be `2026-06-20T15:00:00Z`

#### Scenario: last data row Time_Day=6.5 yields cycle + 6 days 12 hours

- **WHEN** the last data row has `Time_Day=6.5`
- **AND** cycle_time is `2026-06-20T12:00:00Z`
- **THEN** the emitted `valid_time` SHALL be `2026-06-27T00:00:00Z` (cycle + 561600 seconds)

#### Scenario: rounding handles non-exact 3h step deterministically

- **WHEN** a Time_Day value such as `0.041666` (≈ 1 hour) is encountered
- **AND** cycle_time is `2026-06-20T12:00:00Z`
- **THEN** the reader SHALL emit `valid_time=2026-06-20T13:00:00Z` (using `int(round(Time_Day*86400))=3600`, NOT `int(Time_Day*86400)=3599`)

#### Scenario: variable name mapping per units contract, with `unit` field

- **WHEN** the reader emits rows from a CSV column
- **THEN** the API `variable` field SHALL be: `Precip→PRCP`, `Temp→TEMP`, `RH→RH`, `Wind→wind`, `RN→Rn`
- **AND** the corresponding `unit` field SHALL be: `PRCP="mm/day"`, `TEMP="degC"`, `RH="0-1"`, `wind="m/s"`, `Rn="W/m^2"`
- **AND** the reader SHALL NOT emit a `Press` variable (source CSV has no Press column)

#### Scenario: N data rows produce 5 × N tuples per default request (no row-count hardcoding)

- **WHEN** the CSV header row 1 says `N\t6\t<start_date>\t<end_date>` for some row count N
- **AND** the data section has N rows
- **THEN** the reader SHALL emit exactly 5 (variable count) × N (row count) tuples for the default request (all variables)

#### Scenario: malformed CSV raises STATION_FORCING_FILE_MALFORMED

- **WHEN** the CSV is missing the header row or contains a non-numeric value where a numeric is expected
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED` and details `{station_id, expected_path, parse_reason}`

#### Scenario: non-finite numeric CSV values are malformed

- **WHEN** the CSV contains `NaN`, `inf`, or a numeric token whose `Time_Day` conversion overflows the datetime range
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED`
- **AND** the response SHALL NOT contain non-finite JSON numeric values

#### Scenario: blank row inside declared data section is malformed

- **WHEN** the CSV header declares `nrow=N`
- **AND** a blank physical row appears inside those N declared data rows
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED`
- **AND** the reader SHALL NOT skip the blank row and backfill it with a later extra row

#### Scenario: declared nrow mismatch raises STATION_FORCING_FILE_MALFORMED

- **WHEN** the CSV header declares `nrow=N`
- **AND** the data section contains fewer or more than N data rows
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED`
- **AND** `details.parse_reason` SHALL identify the row-count mismatch

#### Scenario: CSV reader enforces hard input bounds

- **WHEN** the CSV file exceeds the configured byte cap, a single line exceeds the configured line cap, or the header `nrow` exceeds the configured row cap
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED`
- **AND** the reader SHALL NOT read or parse the full oversized tail after the failure is known

#### Scenario: CSV file is read through no-follow descriptor-bound open

- **WHEN** the expected station CSV path is a symlink, is not a regular file, or violates the reader's containment root
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED`
- **AND** it SHALL NOT follow the symlink target
- **AND** a refusal raised because the target's inode changed while the file was being opened SHALL be excluded from this scenario, being governed instead by the requirement covering concurrent atomic replacement

#### Scenario: file open/read OS errors are mapped to malformed

- **WHEN** opening or reading an existing resolved CSV raises `PermissionError` or a generic `OSError`
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED`
- **AND** `details.parse_reason` SHALL preserve operator-useful error text
