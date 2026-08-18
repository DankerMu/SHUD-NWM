## ADDED Requirements

### Requirement: Default sacct lookback windows are rendered in the host's local wall clock

The gateway SHALL render the default `--starttime` lookback boundary it
passes to sacct in the host's local wall-clock representation of the
UTC-computed instant, because sacct interprets bare timestamps in the
host's local timezone: the default window is computed as UTC now minus
the configured lookback and converted with the host's local timezone
before formatting, so the effective window is the configured width on
every host timezone instead of silently widening east of UTC and
narrowing west of it — except across the once-yearly ambiguous local
hour on DST-observing hosts, where a bare local timestamp is irreducibly
ambiguous to sacct's timezone-less interface and the boundary may land
up to an hour off (inherent to sacct, not to this conversion;
spring-forward is safe because a UTC-to-local conversion never emits a
skipped wall clock). Explicitly supplied start or end times keep their
existing caller-owned semantics without re-conversion, and on a UTC host
the rendered value is byte-for-byte what it was before.

#### Scenario: a negative-offset host keeps the full lookback window

WHEN the host timezone is west of UTC and list_jobs runs with no explicit
start time
THEN the rendered `--starttime` equals the local wall-clock form of UTC
now minus the configured lookback (the window is not narrowed)

#### Scenario: a UTC host renders the same value as before

WHEN the host timezone is UTC
THEN the rendered `--starttime` string is byte-for-byte identical to the
pre-change output

#### Scenario: explicit caller times are not re-converted

WHEN a caller supplies an explicit start time
THEN it is passed through byte-for-byte unchanged
