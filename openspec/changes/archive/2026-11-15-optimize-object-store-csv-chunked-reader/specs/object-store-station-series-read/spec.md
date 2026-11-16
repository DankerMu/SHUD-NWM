## ADDED Requirements

### Requirement: Station CSV line reads are chunked and bounded

The object-store station-series reader SHALL consume per-station SHUD CSV files
through descriptor-bound chunked line reads. It SHALL enforce the configured
total-byte cap, per-line byte cap, and declared row-count cap while reading, and
it SHALL NOT depend on reading the entire file into memory before those bounds
can reject malformed input.

#### Scenario: valid logical lines may span multiple read chunks

- **WHEN** a valid station forcing CSV is read with a chunk size smaller than
  the header row, column row, or data row length
- **THEN** the reader SHALL assemble the split chunks into the same logical
  lines and return the same StationSeriesResponse values as a normal read
- **AND** the read path SHALL perform multiple descriptor reads instead of one
  full-file read

#### Scenario: oversized tails are rejected without full-tail reads

- **WHEN** the reader can determine from the header or row-count contract that a
  CSV is malformed before reading the entire remaining file
- **THEN** it SHALL raise `STATION_FORCING_FILE_MALFORMED`
- **AND** it SHALL NOT read the full oversized tail after the failure is known
