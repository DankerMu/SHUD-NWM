## ADDED Requirements

### Requirement: Non-finite flow values are rejected

The output parser SHALL refuse a `.rivqdown` file that contains a flow value which parses as a
number but is not finite (`NaN`, `Inf`, `-Infinity` and every other spelling the number parser
accepts, and a literal that overflows to infinity). The
refusal SHALL carry the error code `NON_FINITE_FLOW` and name the row, SHALL mark the run failed
with that code, and SHALL write no row of the file to the river time series, including rows that
precede the bad value in the file.

#### Scenario: A NaN or infinite value fails the run without writing rows

- **WHEN** a `.rivqdown` file contains `NaN`, `Inf` or `-Infinity` in a flow column, on its only data row or after valid rows
- **THEN** parsing raises an output-parsing error with code `NON_FINITE_FLOW`
- **AND** the run is recorded as failed with `NON_FINITE_FLOW`
- **AND** no river time-series row of that file is written
