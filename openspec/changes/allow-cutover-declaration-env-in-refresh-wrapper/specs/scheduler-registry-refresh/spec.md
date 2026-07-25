# Spec Delta: scheduler-registry-refresh

## ADDED Requirements

### Requirement: The refresh wrapper SHALL admit the cutover declaration path as an optional environment key without weakening its parse constraints

The systemd refresh wrapper's EnvironmentFile allowlist SHALL accept `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` as an optional (non-required) key and export it to the runner process, so the systemd path can execute declared package cutovers; the key's absence SHALL leave wrapper behavior unchanged (runner refuses undeclared cutovers), and every other wrapper safety constraint (0600 mode, symlink refusal, DB-selector refusal, newline and duplicate refusal, required-key set, direct-grid assertion) SHALL remain in force.

#### Scenario: EnvironmentFile carrying a declaration path passes the wrapper

- **WHEN** the mode-0600 EnvironmentFile contains
  `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=<absolute path>` alongside the
  required refresh keys
- **THEN** the wrapper SHALL parse successfully and the exec'd runner process
  SHALL observe the variable with the exact value, under the same name the
  runner reads (`CUTOVER_DECLARATION_ENV`)

#### Scenario: Absent declaration key keeps the safe-refuse default

- **WHEN** the EnvironmentFile omits `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`
- **THEN** the wrapper SHALL behave exactly as before this change (exit 0
  with required keys present, variable unset in the runner process), leaving
  the cutover gate to refuse undeclared package cutovers

#### Scenario: No other parse constraint is relaxed

- **WHEN** the EnvironmentFile contains a key outside the allowlist, a DB
  selector, a duplicate key, or a value with a newline
- **THEN** the wrapper SHALL still fail fast exactly as before this change
