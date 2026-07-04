# archive-rebuild-drill Specification (delta)

## ADDED Requirements

### Requirement: Drill proves reconstruction via the existing ingest path

The rebuild drill SHALL restore at least one archived product cycle's files
into a staging location and reingest them using the existing node-27 ingest
code path (configured to write the drill's isolated staging schema), then
compare the resulting staging row counts per (run, variable) against
expected counts derived by parsing the restored product files with the same
parsing logic the ingest path uses. Product-archive manifests record file
checksums and identity, not row counts, so file-derived counts are the only
parity oracle for product cycles. For salvaged `db-export` objects the drill
SHALL verify instead of reingest: per selector, the object's sha256 MUST
match its manifest and the decompressed row count MUST equal the manifest's
recorded exported row count (see `db-export-salvage`: these objects have no
automated restore lane).

#### Scenario: Parity passes

- **WHEN** every compared product cycle's staging counts per (run, variable)
  exactly match the counts parsed from its restored files, and every compared
  `db-export` selector's checksum and decompressed row count match its
  manifest
- **THEN** the drill MUST emit a PASS receipt naming the cycles, selectors,
  and counts compared

#### Scenario: Parity fails

- **WHEN** any compared (run, variable) or `db-export` selector mismatches
- **THEN** the drill MUST emit a FAIL receipt with the per-item diff and
  exit non-zero

### Requirement: Drill reingest is isolated from production hypertables

The drill SHALL restore and reingest only into a dedicated staging
schema/database provisioned with the same DDL as the production hypertables
and with no compression enabled; it SHALL NOT write the production
hypertables. Isolation makes staging counts attributable solely to
drill-restored data (pre-existing production rows can never satisfy parity)
and guarantees the drill never triggers the compressed-chunk fail-closed
guard defined in `hypertable-compression` — an exemption by isolation, not a
bypass. The staging schema SHALL be recorded in the receipt and reset per
drill run.

#### Scenario: Production tables unchanged by a drill

- **WHEN** a drill run completes (PASS or FAIL)
- **THEN** production hypertable row counts MUST be unchanged by the drill
- **AND** the receipt MUST record the staging schema/database identity used

#### Scenario: Drill runs regardless of production compression state

- **WHEN** the production chunks covering a drilled cycle's window are
  compressed
- **THEN** the drill MUST still complete without decompressing or writing
  any production chunk

### Requirement: Drill receipts declare their coverage

Every drill receipt SHALL declare the validated (source, time window)
tuples. A drill PASS receipt covers a candidate retention drop window only
when its declared tuples include, sampled from within or older than that
drop window: at least one product-derived cycle for each timeseries-bearing
source lane (`forcing/`, `runs/`) that has DB rows in the drop window, plus
at least one `db-export` selector whenever verified salvage objects cover
any part of the drop window. The retention gate SHALL evaluate coverage
against these declared tuples.

#### Scenario: Declared coverage does not include the drop window

- **WHEN** retention enforce evaluates a drill PASS receipt whose declared
  (source, window) tuples do not satisfy the coverage rule for the candidate
  drop window
- **THEN** retention enforce MUST refuse and record the coverage shortfall
  in its refusal receipt

### Requirement: Drill gates retention

The retention runner's drill gate SHALL consume the latest drill receipt; a
FAIL receipt, a stale receipt (older than the configured validity window),
or a PASS receipt whose declared coverage does not include the candidate
drop window SHALL block retention enforcement.

#### Scenario: Failed drill blocks retention

- **WHEN** the latest drill receipt is FAIL or older than the configured
  validity window
- **THEN** retention enforce MUST refuse to run

### Requirement: Drill executions are receipted on node-27

Drill runs SHALL execute on node-27 against the real archive and the real
database instance (writing only the isolated staging schema), and their
receipts SHALL be committed to the repository's runbook receipts.

#### Scenario: Committed drill receipt

- **WHEN** a drill run completes on node-27
- **THEN** its receipt (PASS or FAIL) MUST be committed under the runbook
  receipts directory
