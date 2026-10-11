## ADDED Requirements

### Requirement: Frontend type checks SHALL reject unused locals and parameters

The frontend type-check gates (`pnpm typecheck` and `pnpm run check:types`) SHALL run with `noUnusedLocals` and `noUnusedParameters` enabled, so a non-exported local declaration, an import binding or a parameter that nothing reads fails the existing Frontend Build lane without any additional tool or CI step. This complements, and does not replace, the requirement that `tsc` runs as its own gated step.

#### Scenario: A change leaves an unused local behind

- **WHEN** a source file covered by the app type-check declares a local or imports a binding that is never read
- **THEN** `pnpm typecheck` fails with the unused-declaration diagnostic

#### Scenario: A parameter must stay for its position

- **WHEN** a function has to keep a parameter it does not read because of its position or a callback signature
- **THEN** the parameter is named with a leading underscore and the type-check passes
