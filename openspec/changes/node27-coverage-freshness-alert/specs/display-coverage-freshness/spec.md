## ADDED Requirements

### Requirement: A coverage freshness observer MUST alert when the national catalog's frontier falls behind ingest

node-27 SHALL run a read-only coverage freshness check that, per source key
`COALESCE(lower(source_id), '__null_source__')`, compares the **display-ready frontier** —
`max(cycle_time)` over `hydro.hydro_run` rows with `status IN ('succeeded','parsed','published')`
and `cycle_time IS NOT NULL`, joined to a `core.model_instance` row with `active_flag` and a
non-NULL `river_network_version_id` — against the **covered frontier**, which SHALL be obtained
from the display module's own catalog owner (`services/tiles/mvt.py` `national_discharge_cycles`,
field `default_cycle`) rather than re-derived, so the observer cannot drift from the surface it
watches. A source SHALL be evaluated only when its display-ready frontier is not older than
`NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`; the `__null_source__` key SHALL never be evaluated.
The check SHALL exit non-zero and name the offending sources when an evaluated source's gap
exceeds the threshold or it has no covered cycle at all, and its systemd unit SHALL route that
non-zero exit to the node-27 unit-failure mail handler with the verdict inside the journal tail
that handler mails. The threshold SHALL be derived from `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`
rather than a hard-coded day count, and SHALL be rejected as a configuration error when it is
not a finite number strictly greater than zero and strictly smaller than that constant. The
check SHALL hold no persistent state.

#### Scenario: Coverage stall is detected before the layer goes dark

- **WHEN** a source's display-ready frontier keeps advancing while its covered frontier stays
  behind by more than the threshold
- **THEN** the check exits non-zero, names that source with its gap in days, and the unit's
  `OnFailure=` handler mails the operator

#### Scenario: Outcome-based, not return-code-based

- **WHEN** every coverage refresh returned rc=0 but produced no advancing coverage row (the
  `no_coverage_row` path)
- **THEN** the check still exits non-zero, because its criterion reads only the two frontiers
  and never a refresh return code

#### Scenario: Populated but unlistable coverage rows are not counted as covered

- **WHEN** a cycle's coverage rows exist with `segment_count > 0` but the catalog refuses to
  list that cycle — incomplete active-network coverage, inconsistent coverage window columns,
  or no valid time on the published stride
- **THEN** the covered frontier used by the check is the catalog's own newest listed cycle, so
  the check reports the gap the layer will actually show

#### Scenario: Full ingest stall does not double-alert

- **WHEN** both frontiers stop advancing together because ingest has stalled
- **THEN** the gap does not grow, the check exits zero, and the condition remains owned by the
  `frontier-stalled` lane

#### Scenario: A source outside the display window is not evaluated

- **WHEN** a source's display-ready frontier is older than `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`,
  or the source key is `__null_source__`
- **THEN** that source is reported as not evaluated and cannot by itself make the check exit
  non-zero

#### Scenario: Nothing observable is fail-closed

- **WHEN** the display-ready frontier query returns no source key at all, so the check cannot see
  the run set the catalog is built from
- **THEN** the check exits non-zero rather than reporting health, because the frontier-stall lane
  does not observe river-network activity and would stay silent through the same condition

#### Scenario: Threshold at or beyond the display window is refused

- **WHEN** the configured threshold is non-finite, zero, negative, or at least
  `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`
- **THEN** the check exits with a configuration error before observing anything, rather than
  clamping the value

#### Scenario: Observation failure is fail-closed and redacted

- **WHEN** the observation cannot run (database unreachable, statement timeout, or permission
  denied)
- **THEN** the check exits non-zero so the failure is mailed, and the reported error contains no
  DSN password

#### Scenario: The verdict survives the mailed journal tail

- **WHEN** more sources exist than the report may print
- **THEN** the report is truncated with an explicit omission line, breaching sources are kept,
  and the verdict block is printed last so it remains inside the journal tail the failure
  handler mails
