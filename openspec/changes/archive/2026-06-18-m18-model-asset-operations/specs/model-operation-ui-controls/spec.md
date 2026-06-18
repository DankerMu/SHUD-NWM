## ADDED Requirements

### Requirement: Model Operation UI Controls
The model asset UI SHALL expose lifecycle controls only for authorized roles and show preflight/audit outcomes clearly.

#### Scenario: Authorized model admin
WHEN a model_admin views a selected model
THEN activate/deactivate/switch/rollback controls appear according to current state and preflight availability.

#### Scenario: Unauthorized viewer
WHEN a viewer or analyst views the same page
THEN mutating controls are hidden or disabled and backend denial remains authoritative.

#### Scenario: Preflight blocker
WHEN a user attempts an operation with a preflight blocker
THEN the UI shows blocker details and leaves model state unchanged.
