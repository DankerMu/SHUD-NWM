## MODIFIED Requirements

### Requirement: Production RBAC is not user-spoofable

The frontend SHALL NOT expose a production control that lets users grant themselves operator/admin roles.

#### Scenario: Production role source is fixed

- **WHEN** the app runs in production mode without an explicit dev/test role override
- **THEN** the role selector MUST NOT be shown
- **AND** no retry or cancel control exists for any role, so none can become shown or actionable because `VITE_AUTH_ROLE` is configured as an operator/admin role
- **AND** operator action headers MUST only be sent by the explicit dev/test role override path until a trusted production backend auth/session mechanism exists
