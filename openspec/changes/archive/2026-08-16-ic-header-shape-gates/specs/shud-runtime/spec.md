# shud-runtime (delta)

## ADDED Requirements

### Requirement: IC Time Shift Fails Closed on Malformed Headers

The runtime IC time-shift step SHALL refuse to rewrite an existing,
non-empty `.cfg.ic` whose header line carries fewer than three numeric
tokens: the file SHALL be left byte-identical and a structured runtime
error SHALL surface, instead of silently overwriting a non-minute-time
token. A missing or empty `.cfg.ic` keeps the existing no-op (those are
legitimate cold-start and diagnostic-manifest states). Headers with three
or more numeric tokens keep the existing shift-the-last-numeric-token
behavior byte-for-byte, including the four-token compatibility layout. On
the warm-start materialization path the structured error SHALL be
translated into the existing corrupted-state rejection so the snapshot
degradation ladder (next usable state, cold-start fallback) keeps running;
on the forcing-staging paths the error surfaces through the existing
visible error channel.

#### Scenario: A two-token header aborts the run visibly instead of corrupting the column count

- **GIVEN** a staged `.cfg.ic` whose header line is `23106\t6`
- **WHEN** workspace preparation reaches the IC time-shift step
- **THEN** the file bytes are unchanged and workspace preparation fails with
  a structured runtime error naming the malformed header — the column count
  is never overwritten with an epoch-minute value

#### Scenario: Native and compatibility layouts shift exactly as before

- **GIVEN** a `.cfg.ic` header with three numeric tokens (native layout) or
  four numeric tokens (compatibility layout)
- **WHEN** the IC time-shift step runs
- **THEN** the resulting file is byte-identical to the pre-change behavior:
  only the trailing numeric token is replaced with the aligned minute-time

#### Scenario: A single-numeric-token header is no longer silently preserved

- **GIVEN** an existing, non-empty `.cfg.ic` whose header line carries
  fewer than two numeric tokens
- **WHEN** the IC time-shift step runs
- **THEN** the step fails closed with the same structured error instead of
  silently skipping the shift

#### Scenario: A malformed warm-start snapshot degrades instead of failing the run

- **GIVEN** a warm-start candidate snapshot whose IC header is malformed in
  a form that reaches the time-shift step — zero or one numeric token, or a
  snapshot without a recorded valid time (a two-token header on a snapshot
  with a recorded valid time keeps hitting the pre-existing time-consistency
  failure first; that behavior is unchanged by this change)
- **WHEN** warm-start materialization hits the time-shift shape error
- **THEN** the snapshot is marked corrupted through the existing rejection
  channel and the run continues down the degradation ladder (next usable
  snapshot or cold start) instead of failing outright

#### Scenario: A missing or empty IC keeps the existing no-op

- **GIVEN** a workspace whose `.cfg.ic` is absent or zero-length
- **WHEN** the IC time-shift step runs
- **THEN** the step is a no-op exactly as before this change
