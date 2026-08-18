## ADDED Requirements

### Requirement: Committed historical compression receipts MUST stay schema-valid under test

The four committed historical compression runner receipts SHALL be validated
against `schemas/timeseries_compression_receipt.schema.json` by a committed
parametrized test that globs the receipts directory by runner filename prefix
(excluding the co-located live-evidence family) and asserts the glob is
non-empty, so schema tightening that invalidates the archive fails loudly.

#### Scenario: schema tightening goes red

WHEN the schema's 1.0 branch gains a new required field
THEN the committed 1.0 receipts fail the parametrized validation test

#### Scenario: mistyped glob cannot fake-green

WHEN the receipt glob matches fewer than four files
THEN the count-guard test fails
