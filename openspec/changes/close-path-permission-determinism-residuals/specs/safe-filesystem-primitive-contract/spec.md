## MODIFIED Requirements

### Requirement: The shared safe-filesystem primitives SHALL report an undeterminable home directory as a structured refusal

Every public entry point of the shared safe-filesystem module SHALL surface a failure to expand a leading `~` component as the module's own structured error carrying its `kind` classification, never as a bare errno-less `RuntimeError`, because that expansion is the shared prelude of all of them and a bare throw defeats the module's advertised error contract on every caller at once.

The refusal SHALL reuse the existing `unsafe` classification rather than introducing a new one, so that callers which branch on the existing classification values keep a total set of branches. The expansion failure SHALL NOT be degraded into a permissive pass that keeps the literal `~` component, because the write-side primitives would then create, delete, or overwrite a path the operator never named.

#### Scenario: A write-side primitive refuses an undeterminable home directory without touching the filesystem

- **GIVEN** a path whose leading component names a user for whom no home directory can be determined
- **WHEN** the directory-creating primitive is invoked with it
- **THEN** it raises the module's structured filesystem error carrying the `unsafe` classification
- **AND** it does not raise a bare `RuntimeError`
- **AND** no directory whose name literally begins with `~` is created anywhere under the working directory

#### Scenario: The read-side and delete-side primitives refuse the same input identically

- **GIVEN** the same undeterminable-home path
- **WHEN** the size-limited read primitive and the recursive-delete primitive are each invoked with it
- **THEN** each raises the module's structured filesystem error carrying the `unsafe` classification
- **AND** neither leaves a literal `~`-prefixed entry behind

#### Scenario: A CLI configuration lane converts the refusal into its own structured rejection

- **GIVEN** a command-line environment-file argument whose value has an undeterminable home directory
- **WHEN** the environment file is applied
- **THEN** the lane reports its existing structured configuration rejection
- **AND** no bare `RuntimeError` escapes to the operator as a traceback

#### Scenario: An evidence-root preparation lane converts the refusal into its own structured code

- **GIVEN** an evidence root whose configured value has an undeterminable home directory
- **WHEN** the lane prepares its evidence directories
- **THEN** it reports its existing structured evidence error code rather than aborting with a bare `RuntimeError`

#### Scenario: An evidence-root configuration and validation lane converts the refusal into its own structured code

- **GIVEN** an evidence root whose configured value has an undeterminable home directory
- **WHEN** `ProductionMetConfig.from_env` resolves the root, and when `validate_met` revalidates an equivalent config
- **THEN** each entrypoint reports `PRODUCTION_MET_EVIDENCE_PATH_UNSAFE` rather than aborting with a bare `RuntimeError`
- **AND** neither creates a literal `~`-prefixed entry under the working directory

## ADDED Requirements

### Requirement: Write-side configured-path wrappers SHALL preserve their owning structured error contracts

A production wrapper that expands a configured path before invoking shared write-side filesystem primitives SHALL translate an undeterminable-home failure into its existing owning-module error contract before any filesystem access. It SHALL NOT leak an errno-less `RuntimeError`, retain a literal `~` component, or add a new public error code where an existing path/write refusal already represents the failure.

#### Scenario: Published log paths reject an undeterminable artifact root at every public chain seam

- **GIVEN** `NHMS_PUBLISHED_ARTIFACT_ROOT` names a user whose home directory cannot be determined
- **WHEN** gateway-log persistence, local-stage-log writing, or published-log path derivation consumes that root
- **THEN** each seam raises the orchestrator's existing `PUBLISHED_LOG_WRITE_FAILED` error
- **AND** no bare `RuntimeError` escapes
- **AND** no literal `~`-prefixed path is created under the working directory

#### Scenario: Valid configured roots retain their existing products

- **GIVEN** an existing absolute published-artifact root and an existing absolute production-met evidence root
- **WHEN** their respective wrappers resolve and use those roots
- **THEN** the resulting paths and successful write behavior are unchanged from before this change