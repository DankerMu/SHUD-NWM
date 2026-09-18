## MODIFIED Requirements

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
- **THEN** the report is truncated with an explicit omission line that states exactly how many
  rows it dropped, and the verdict block is printed last and is never itself truncated, so it
  remains inside the journal tail the failure handler mails
- **AND** breaching sources are ordered ahead of the others, so the rows dropped first are the
  non-breaching ones; the guarantee is bounded by the capacity of the truncated table (one of
  its rows goes to the omission line), and once breaching sources alone exceed it, breaching
  rows are dropped from the table as well
- **AND** no truncation is silent about what it dropped: the report header states the true
  count of breaching sources, and the verdict names a bounded number of them followed by the
  count of the rest when any remain

### Requirement: Coverage freshness failure stages SHALL be attributable by exit code

The coverage freshness check SHALL distinguish its two failure stages by exit code, and the operator documentation SHALL route each stage to a remediation that can actually close it. A failure to import the display module happens in the configuration stage, before any database work, and SHALL therefore be reported as a configuration failure; a display-module failure raised during observation SHALL be reported as an observation failure, alongside database unreachability, statement timeout and permission denial. Live operator documentation SHALL NOT describe "display-module error" without naming the stage it belongs to. The operator documentation SHALL name every structured failure code the check can emit and give each a first step that can close it; where one code covers several causes, it SHALL name the discriminator the mailed failure line already carries — the exception class that prefixes the reason, and the driver error class that follows it — rather than routing every cause of that code to a single remediation. An archived change record is exempt: it is the record of what was decided, and it is corrected by an appended dated note rather than by rewriting the original row.

#### Scenario: Import-time display failure is a configuration failure

- **WHEN** the display module cannot be imported (for example the unit's `PYTHONPATH` is missing, or the virtualenv lacks the display stack)
- **THEN** the check exits with the configuration exit code before attempting any observation, emits its structured configuration error line, and the runbook's row for that exit code names this case with a first step that addresses the interpreter path rather than the threshold knob

#### Scenario: Every structured failure code has a documented destination

- **WHEN** the check's structured failure codes are compared against the lane's operator documentation
- **THEN** every code the check can emit is named there with a first step, and a regression test fails if a code is added to the check without being named there

#### Scenario: One observation-failure code, several causes, routed by what the mail carries

- **WHEN** an observation failure is mailed because the database is unreachable, a statement timed out, permission was denied, the configured database lacks the lane's relations, or the display module raised during observation
- **THEN** the documentation routes each of those causes to its own first step using the reason's exception-class prefix and the driver class that follows it, and a cause that matches none of them has an explicit fallback rather than landing on another cause's remediation

### Requirement: Every node-27 coverage freshness unit SHALL be registered in the governance liveness inventory

The coverage freshness service and timer SHALL appear in the node-27 resource-governance audit's default service inventory, and the audit SHALL collect, for every unit in that inventory, the unit's load state and unit-file state, so that a disabled or masked timer is distinguishable in the governance receipt from one that was never installed, rather than being visible only in the alerting lane's own absence of mail. Registration makes that state visible for periodic human reading; it is not an alert, and the documentation SHALL NOT describe it as one. The installation documentation for the lane SHALL state that registration as part of its closure, as the sibling frontier-stall lane's documentation does.

#### Scenario: A disabled timer is visible to the governance oracle

- **WHEN** the governance audit collects systemd state over its default service inventory
- **THEN** the receipt carries an entry for both the coverage freshness service and its timer, including each unit's load state and unit-file state, so a timer that was installed and later disabled reads differently there from one that was never installed

#### Scenario: Registration is visibility, not an alert

- **WHEN** a registered timer is disabled
- **THEN** the governance audit's exit code and recommendations are unchanged, and the lane's documentation states that a disabled timer is found by reading the receipt, not by an alert
