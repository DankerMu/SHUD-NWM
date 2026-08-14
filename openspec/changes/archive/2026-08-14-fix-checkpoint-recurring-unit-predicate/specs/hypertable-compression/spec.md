# hypertable-compression（delta）

## ADDED Requirements

### Requirement: Mutation-window checkpoints MUST gate the recurring unit on current-activity facts, never on boot history

The supervisor checkpoint and its live-evidence verifier counterpart SHALL judge the recurring compression unit (`nhms-node27-timeseries-compression.service`) safe for a mutation window using only current-activity and identity facts — fragment path, `ActiveState`, `SubState`, and `MainPID` — with both planes applying an identical predicate. Fields that record boot history (`ExecMainStartTimestamp`, `ExecMainStartTimestampMonotonic`, and `InvocationID`, which systemd retains on a loaded unit after it returns to inactive — measured on node-27 2026-08-14) SHALL remain captured in the checkpoint evidence document but SHALL NOT participate in the gating decision. Predicate failures SHALL name the diverging fields; a `SubState=failed` unit SHALL produce a distinct message naming `reset-failed` as the remedy, and no failure text SHALL describe boot history as concurrent activity.

#### Scenario: unit ran earlier this boot and is now inactive

- **WHEN** the recurring unit reports `ActiveState=inactive`, `SubState=dead`, `MainPID=0`, the pinned fragment path, and boot-history fields retained from an earlier timer tick (non-unset timestamps, non-empty `InvocationID`)
- **THEN** both the supervisor checkpoint and the live-evidence verifier SHALL pass the recurring-unit gate, and the evidence document SHALL still carry all three boot-history fields verbatim

#### Scenario: unit is currently active, failed, or identity-drifted

- **WHEN** the recurring unit reports `ActiveState` other than `inactive`, or `SubState` other than `dead`, or a non-zero `MainPID`, or a diverging fragment path
- **THEN** both planes SHALL fail closed with an error that names the diverging field(s), and the `SubState=failed` case SHALL name `reset-failed` as the operator remedy

#### Scenario: evidence document omits boot-history fields

- **WHEN** a checkpoint show document lacks `ExecMainStartTimestamp`, `ExecMainStartTimestampMonotonic`, or `InvocationID`, or carries them with wrong types
- **THEN** the live-evidence verifier SHALL reject the document as malformed evidence
