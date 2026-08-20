# safe-filesystem-primitive-contract Specification

## Purpose

The error contract of `packages/common/safe_fs.py`, the shared write-side filesystem primitive
layer that every lane funnels its directory creation, size-limited reads, and recursive deletes
through. This capability governs what those primitives are allowed to throw, not what they are
allowed to touch: a failure inside a primitive must arrive at the caller as the module's own
structured error carrying a `kind` classification, never as a bare stdlib exception that no
caller's `except` tuple was written for. It exists because the module is a shared base — a single
unstructured throw defeats the advertised contract on all of its callers at once — and because
these are *write-side* primitives, where the alternative to refusing is acting on a path the
operator never named.

## Requirements
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

The evidence-root lane's own separate expansion, which sits outside the shared module, is not covered by this requirement and continues to throw bare; it is tracked on its own.

#### Scenario: An evidence-root preparation lane converts the refusal into its own structured code

- **GIVEN** an evidence root whose configured value has an undeterminable home directory
- **WHEN** the lane prepares its evidence directories
- **THEN** it reports its existing structured evidence error code rather than aborting with a bare `RuntimeError`

