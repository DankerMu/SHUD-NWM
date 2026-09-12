## ADDED Requirements

### Requirement: The refresh timer's liveness is observable from independent signals before the freshness bound expires

The system SHALL provide a read-only, database-free health probe for the node-22
file-provider refresh lane that records the timer's unit-file state, active
state, next-elapse time, and the published manifest's age as four independent
signals, grades them into exactly one verdict, writes a bounded receipt, and
exits non-zero for every verdict other than healthy. The verdicts SHALL be
evaluated in a fixed precedence order — unreadable systemd evidence, then
expired manifest, then stopped timer, then not-enabled timer, then unscheduled
timer, then stale manifest, then unresolvable manifest — with the first matching
condition winning and the healthy verdict reachable only when no other condition
matches. An unresolvable manifest age SHALL NOT mask any timer verdict, and the
two manifest-age comparisons SHALL be skipped rather than defaulted when no age
could be resolved. The probe SHALL NOT
enable, disable, start, stop, restart, or reload any systemd unit, and SHALL NOT
write any file under the provider store.

#### Scenario: A stopped but still-enabled timer is detected

- **WHEN** the refresh timer reports an `ActiveState` other than `active`, the
  elapsed time since it became inactive exceeds the configured stopped-dwell,
  and the published manifest has not yet reached the consumer's 168-hour bound
- **THEN** the probe returns the `timer_stopped` verdict and a non-zero exit status
- **AND** the receipt records the unit-file state and active state as separate
  fields, so `enabled` alone can never be read as proof of liveness
- **AND** no systemd unit state is modified by the probe

#### Scenario: A deliberately stopped timer inside the operator dwell does not alarm

- **WHEN** the refresh timer reports `UnitFileState=enabled` with an
  `ActiveState` other than `active`, but the elapsed time since it became
  inactive is within the configured stopped-dwell threshold — the geometry of a
  live manual-publisher window, where the documented procedure stops the timer,
  runs the publisher, and starts it again
- **THEN** the probe does not return the `timer_stopped` verdict
- **AND** the remaining signals are still graded, so a stale or expired manifest
  in the same tick still produces a non-healthy verdict and a non-zero exit

#### Scenario: A timer left un-enabled is detected rather than graded healthy

- **WHEN** the refresh timer reports a `UnitFileState` other than `enabled` —
  including the `disabled` state the refresh installer deliberately leaves
  behind after an install or a rollback — and no higher-precedence condition
  matches
- **THEN** the probe returns the `timer_not_enabled` verdict and a non-zero exit status
- **AND** the healthy verdict is not reachable for such a timer, whether or not
  it happens to be active at that moment

#### Scenario: An enabled and active timer with no scheduled tick is detected

- **WHEN** the refresh timer is `active` but reports no next-elapse time, or a
  next-elapse time further away than the configured next-dwell threshold, and no
  higher-precedence condition matches
- **THEN** the probe returns the `timer_not_scheduled` verdict and a non-zero exit status

#### Scenario: Manifest age is graded independently of systemd state

- **WHEN** the most recent published provider manifest's generation time is older
  at or beyond the configured manifest-age threshold but not yet at the
  consumer's 168-hour bound, and no higher-precedence condition matches
- **THEN** the probe returns the `manifest_stale` verdict and a non-zero exit status
- **AND** when that age reaches or exceeds 168 hours the probe returns the
  distinct `manifest_expired` verdict
- **AND** every configurable threshold is range-checked so that it always leaves
  at least one full refresh cadence of margin below the consumer's 168-hour
  bound, and the stopped-dwell is additionally capped at one cadence; a
  configuration outside those ranges is rejected before any evidence is
  collected, and no accepted combination of thresholds grades a lane healthy
  once it has been idle for longer than one cadence

#### Scenario: The probe fails closed

- **WHEN** the systemd query cannot be executed or returns an error, or the
  timer is not active while reporting no parseable time at which it became
  inactive — leaving the stopped-dwell comparison undefined
- **THEN** the probe returns a non-healthy verdict and a non-zero exit status
- **AND** the healthy verdict is never produced as a fallback for missing evidence

#### Scenario: A failed refresh receipt does not mask a dead timer

- **WHEN** the latest refresh receipt cannot yield a published manifest's
  generation time — it is missing, unreadable, schema-invalid, or carries no
  registry provider, which is the shape a refresh run that fails before it
  assembles its provider list leaves behind — and the refresh timer is in a
  state that would otherwise grade as stopped or not enabled
- **THEN** the probe returns the timer verdict, not an evidence verdict, so the
  actionable fact is the one reported
- **AND** the manifest-age comparisons are skipped rather than treated as
  satisfied or violated

#### Scenario: The manifest age falls back to receipt history

- **WHEN** the latest refresh receipt cannot yield a generation time, but an
  earlier receipt in the runner's bounded history directory can
- **THEN** the probe resolves the manifest age from the most recent such
  receipt, ordered by the fixed-width UTC timestamp their filenames carry
- **AND** the receipt records which source answered, distinguishing the latest
  receipt, a named history receipt, and no available source
- **AND** the scan is bounded in both the number of directory entries considered
  and the number of receipts opened, and each read refuses symlinks and is size-limited

#### Scenario: No resolvable manifest age is its own verdict

- **WHEN** neither the latest refresh receipt nor any receipt in the bounded
  history scan yields a published manifest's generation time, and no
  higher-precedence condition matches
- **THEN** the probe returns a distinct unresolvable-manifest verdict and a
  non-zero exit status
- **AND** the healthy verdict is not reachable, and the recorded source is the
  no-source value

#### Scenario: The compute scheduler units are untouched

- **WHEN** the probe or its installer runs in any mode
- **THEN** the compute scheduler's timer is identical before and after the run in
  both its unit-file state and its active state
- **AND** the compute scheduler's service — a oneshot activated by that timer on
  its own cadence, and therefore not a unit whose active state any installer can
  hold still — is identical before and after the run in its unit-file state
- **AND** the comparison is performed per unit type for this reason, so that a
  oneshot activating on schedule mid-run is never mistaken for an installer
  having touched it
- **AND** the installer's protected-state baseline is captured at the start of
  every invocation, so the comparison always describes that one invocation and
  no on-disk baseline is ever interpreted across versions of the installer

### Requirement: The tracked DB-free scheduler env template carries every key the documentation declares mandatory

The tracked template for the node-22 database-free scheduler environment SHALL
contain every environment key that the environment documentation declares
mandatory for that lane, including the orchestrator terminal stage that stops
the chain before the database-dependent publish stage.

#### Scenario: A node rebuilt from the template stops at the documented terminal stage

- **WHEN** the node-22 database-free scheduler environment is rebuilt from its
  single tracked template
- **THEN** the template sets the orchestrator terminal stage to
  `forecast_state_save_qc` and requires forecast warm start
- **AND** every key the environment documentation names as mandatory for this
  lane is present in the template, verified by an automated check rather than by
  manual reading

### Requirement: A dry-run refresh produces self-valid evidence without mutating any provider

The refresh runner SHALL, in dry-run mode under direct-grid authority with a
worker registry mirror configured, report the canonical registry and the worker
registry mirror with the same prospective model count, produce a terminal
receipt that passes its own validation, persist that receipt to both history and
latest, release its emergency reservation, and leave every provider byte
unchanged. Dry-run mode SHALL NOT commit any provider bytes and SHALL NOT
execute the worker-mirror publisher, and a failure occurring after the precommit
gate SHALL retain its own failure reason.

#### Scenario: Dry-run with a configured worker mirror succeeds

- **WHEN** a dry-run refresh runs with direct-grid authority required, a worker
  registry mirror configured, and canonical and mirror preimages identical
- **THEN** the run exits successfully with `outcome=dry_run`,
  `reason=dry_run_complete`, and `phase=complete`
- **AND** the receipt reports the registry and `registry_worker_mirror`
  `entry_count` values as equal to the prospective model count derived from this
  run, for a single-model and a multi-model set alike
- **AND** the registry, worker mirror, readiness, and state files are byte- and
  preimage-identical before and after the run
- **AND** the receipt is present in both the history directory and the latest
  pointer, and this run's emergency reservation is removed without leaving an
  empty reserved slot
- **AND** a dry-run over an empty model set continues to fail closed with
  `provider_invalid` on both the direct-grid and the non-direct-grid path,
  terminating as a failed outcome carrying no provider evidence, so no
  successful zero-count dry-run receipt is ever produced

#### Scenario: A dry-run failure after the gate keeps its own reason

- **WHEN** a dry-run refresh fails after the precommit gate for a genuine
  provider or gate reason
- **THEN** the reported reason is that genuine reason
- **AND** it is not replaced by a receipt-assembly failure such as
  `primary_receipt_failed`
