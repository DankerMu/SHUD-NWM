## ADDED Requirements

### Requirement: Coverage freshness failure stages SHALL be attributable by exit code

The coverage freshness check SHALL distinguish its two failure stages by exit code, and the operator documentation SHALL route each stage to a remediation that can actually close it. A failure to import the display module happens in the configuration stage, before any database work, and SHALL therefore be reported as a configuration failure; a display-module failure raised during observation SHALL be reported as an observation failure, alongside database unreachability, statement timeout and permission denial. Live operator documentation SHALL NOT describe "display-module error" without naming the stage it belongs to. An archived change record is exempt: it is the record of what was decided, and it is corrected by an appended dated note rather than by rewriting the original row.

#### Scenario: Import-time display failure is a configuration failure

- **WHEN** the display module cannot be imported (for example the unit's `PYTHONPATH` is missing, or the virtualenv lacks the display stack)
- **THEN** the check exits with the configuration exit code before attempting any observation, emits its structured configuration error line, and the runbook's row for that exit code names this case with a first step that addresses the interpreter path rather than the threshold knob

### Requirement: Every node-27 coverage freshness unit SHALL be registered in the governance liveness inventory

The coverage freshness service and timer SHALL appear in the node-27 resource-governance audit's default service inventory, so that a disabled or masked timer is visible in the governance receipt rather than only in the alerting lane's own absence of mail. The installation documentation for the lane SHALL state that registration as part of its closure, as the sibling frontier-stall lane's documentation does.

#### Scenario: A disabled timer is visible to the governance oracle

- **WHEN** the governance audit collects systemd state over its default service inventory
- **THEN** the receipt carries an entry for both the coverage freshness service and its timer, so a timer that was never enabled or was later disabled is observable there
