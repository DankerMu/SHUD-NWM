## ADDED Requirements

### Requirement: Active registered basin/model discovery

The scheduler SHALL discover all runnable active SHUD model instances from the model registry by default and include basin id, basin version id, river network version id, model package URI, model resource profile, and display/frequency capability metadata in each candidate.

#### Scenario: discover active models

WHEN the scheduler scans for candidates
THEN every active registered SHUD model instance with complete basin, river network, and package references is selected unless an explicit operator filter is supplied
AND inactive, deprecated, incomplete, or duplicate active model identities are excluded with structured reasons.

#### Scenario: explicit subset filter

WHEN an operator supplies a basin or model filter
THEN only matching runnable model instances are selected
AND the filter expression and excluded runnable model count are recorded in pass evidence.

#### Scenario: qhh is not special-cased

WHEN qhh is one of several registered runnable basins
THEN it is selected through the same registry contract as other basins
AND the production scheduler does not call qhh-specific shell scripts.

### Requirement: Multi-source cycle discovery

The scheduler SHALL discover candidate forecast cycles for GFS and IFS using per-source availability, lag, horizon, and max-cycles settings.

#### Scenario: source-specific availability

WHEN an IFS cycle is not yet published but a GFS cycle is available
THEN the IFS candidate is recorded with an unavailable or blocked reason code in scheduler evidence or pipeline event details
AND the GFS candidate can still proceed.

#### Scenario: deterministic candidate identity

WHEN the same source, cycle, model, and scenario are discovered by two scans
THEN both scans resolve the same candidate identity, run id, and forcing version id
AND no duplicate work item is created.
