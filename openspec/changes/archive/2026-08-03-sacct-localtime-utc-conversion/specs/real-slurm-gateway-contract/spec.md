# real-slurm-gateway-contract — delta for sacct-localtime-utc-conversion (#1117)

## ADDED Requirements

### Requirement: sacct timestamps enter job records as timezone-aware UTC

Naive sacct timestamps SHALL be interpreted in the gateway process's
local timezone and converted to UTC before entering
`SlurmJobRecord` time fields. `sacct` prints `Start`/`End` as bare
ISO wall-clock strings in the invoking environment's local timezone;
the gateway and its `sacct` subprocess share one TZ environment, so
local-time interpretation is exact. Values carrying an explicit
offset or "Z" suffix are converted (not relabeled) to UTC. The
parser's sentinel semantics (empty/"Unknown"/"None"/"N/A" → absent;
unparseable or unconvertible → the same parse error) are unchanged
in kind.

#### Scenario: Naive local timestamp is converted, not relabeled

- **WHEN** the gateway host runs in a non-UTC timezone (e.g. CST,
  UTC+8) and `sacct` reports a terminal job's `End` as a bare local
  wall-clock string
- **THEN** the parsed `finished_at` is the same instant expressed in
  UTC (local minus the host offset), never the local wall-clock
  digits with a UTC label

#### Scenario: Offset-carrying timestamp is timezone-independent

- **WHEN** a timestamp carries an explicit "Z" or offset suffix
- **THEN** parsing yields the same UTC instant regardless of the
  gateway host's local timezone

#### Scenario: Records never carry naive datetimes

- **WHEN** any sacct-sourced time field is populated on a
  `SlurmJobRecord`
- **THEN** the value is timezone-aware with zero UTC offset, so
  downstream `_ensure_utc`-style consumers take their aware branch
  and the journal's "Z"-suffixed serialization is truthful
