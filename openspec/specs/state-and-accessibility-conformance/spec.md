# state-and-accessibility-conformance Specification

## Purpose
TBD - created by archiving change m15-frontend-visual-conformance. Update Purpose after archive.
## Requirements
### Requirement: State and accessibility conformance
Loading, empty, error, restricted, RBAC-denied, hover/focus, and icon-control states SHALL be visible, named, and consistent.

#### Scenario: State matrix
WHEN required routes are tested for visual conformance
THEN loaded, loading, empty, error, restricted or RBAC-denied, and partial-data states are covered by deterministic e2e assertions or screenshot fixtures

#### Scenario: Extended state matrix
WHEN completed segment detail, meteorology, or model asset routes are included in M15 evidence
THEN missing segment, chart/error, empty stations, station detail unavailable, model-assets denied, loading, and redacted-error states are covered or explicitly narrowed in governance documentation

#### Scenario: Restricted data
WHEN a data source is unavailable or restricted
THEN UI shows warning/restricted state with accessible text and no fake data

#### Scenario: Icon controls
WHEN user tabs through map/page controls
THEN controls have accessible names and visible focus state

#### Scenario: RBAC navigation
WHEN a role lacks access to a restricted page
THEN navigation and direct-route behavior show the documented denied state without exposing protected page content

#### Scenario: Time and fixture-field compatibility
WHEN visual state fixtures exercise forecast, overview, or meteorology pages
THEN existing route query fields, API fixture field names, unit labels, and valid-time controls keep their documented behavior without backend schema changes
