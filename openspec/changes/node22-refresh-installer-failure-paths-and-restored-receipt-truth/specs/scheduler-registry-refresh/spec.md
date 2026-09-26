## MODIFIED Requirements

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
  once its timer has been enabled-but-inactive for longer than one cadence.
  This is deliberately scoped to the stopped-timer geometry: a timer that is
  active and due to tick shortly, whose manifest is merely older than one
  cadence, is healthy by design — the manifest-age threshold exists precisely to
  tolerate a slow-but-alive lane, and capping it at one cadence would defeat it

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
- **AND** a candidate receipt reports the registry generation time the consumer
  would read: the post-publish time only when its outcome guarantees those bytes
  are on disk (published, published with a failed primary receipt, or dry-run),
  and otherwise the pre-run time, so a run that rolled the registry back can never
  make the manifest look fresher than it is
- **AND** the receipt records which source answered, distinguishing the latest
  receipt, a named history receipt, and no available source
- **AND** the scan is bounded in both the number of directory entries considered
  and the number of receipts opened, and each read refuses symlinks and is size-limited

#### Scenario: No resolvable manifest age is its own verdict

- **WHEN** neither the latest refresh receipt nor any receipt in the bounded
  history scan yields a registry generation time it can trust, and no
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

#### Scenario: The probe installer reports a disarmed probe only when it really is

- **WHEN** the probe installer's install has placed the unit files and reloaded
  systemd and the probe timer or service then reads back as enabled or active,
  or its rollback runs and systemd refuses to disable or stop the probe timer or
  service
- **THEN** the run exits non-zero and does not report an installed-stopped or
  rolled-back status
- **AND** success is reported only after re-reading each probe unit on its own
  shows neither is enabled nor active, where a failed probe service left behind
  by a non-healthy verdict counts as disarmed and an unreachable user manager
  does not
- **AND** disarmed means inert rather than removed: rollback restores the unit
  files that preceded the first install recorded in the installer's restore
  baseline and never re-arms them

#### Scenario: The probe installer never disarms an armed probe or rewrites its baseline

- **WHEN** the probe installer's install runs while the probe timer or service
  is enabled or active
- **THEN** it exits non-zero, tells the operator to roll back first, and leaves
  every unit, unit file, and file under the installer's state root unchanged,
  including the per-invocation protected-state capture
- **AND** when it runs against a disarmed probe whose restore baseline is already
  marked complete, it keeps that baseline, so installing twice and then rolling
  back restores the unit files that preceded the first install
- **AND** a baseline whose completion marker is absent is captured afresh, so an
  interrupted first install never leaves a partial baseline in force

## ADDED Requirements

### Requirement: The refresh installer backs out every failure once, reads the result back, and never rewrites its restore baseline

The node-22 file-provider refresh installer SHALL run under `errtrace`. Every
failure after its first mutation SHALL run exactly one restore handler, and
only in the installer's main shell. That handler SHALL attempt every restore
step whatever the earlier steps did, SHALL always finish with read-back
assertions, and SHALL exit non-zero without printing a status line.

Every restore SHALL be confirmed by reading the refresh units back against
their restore target before the installer reports success:

- the timer is compared on unit-file state and active state;
- the service, a timer-driven oneshot, is compared on unit-file state.

The restore target of a rollback and of a failed install SHALL be the recorded
restore baseline. The restore target of a failed enable SHALL be the state that
invocation started from.

The compute-scheduler comparison SHALL be per unit type, captured at the start
of each invocation. It SHALL NOT read any baseline written by another
invocation.

The install action SHALL refuse without mutation while the refresh timer or
service is armed. It SHALL NOT overwrite a restore baseline that is already
recorded.

A recorded unit state that does not have exactly the expected fields and lines
SHALL fail rather than be interpreted.

#### Scenario: A failure before any mutation changes nothing

- **WHEN** an action fails before its first mutation: the current receipt does
  not validate for enable, the install refuses, the install or the rollback
  finds an existing baseline malformed, or the rollback finds its baseline
  missing
- **THEN** the run exits non-zero without a status line, issues no mutating
  systemd call, runs no restore, and changes no unit file or baseline

#### Scenario: A failure inside a function backs the action out

- **WHEN** an assertion or command inside a function fails after the
  installer's first mutation, in install or in enable
- **THEN** the restore handler runs exactly once, in the main shell, including
  when the failing command ran inside a command substitution
- **AND** the run exits non-zero and prints no status line

#### Scenario: A failing restore step does not cut the restore short

- **WHEN** any single restore step fails inside the handler, whether a unit-file
  restore, a daemon reload, or an enable, disable, start, or stop
- **THEN** every later restore step is still attempted
- **AND** both read-back assertions still run
- **AND** the run exits non-zero without a status line

#### Scenario: Rollback success is decided by reading the units back

- **WHEN** a rollback's restore calls all return success but the refresh timer
  reads back in a state other than the recorded baseline
- **THEN** the rollback exits non-zero and does not report rolled back
- **AND** it reports rolled back only when both hold on read-back: the refresh
  units equal the baseline, and the compute-scheduler units are unchanged

#### Scenario: A compute-scheduler oneshot firing mid-run is not a divergence

- **WHEN** the compute scheduler's service activates on its own timer while
  an installer action is running
- **THEN** the action does not abort, because that service is compared on its
  unit-file state only
- **AND** the compute scheduler's timer is still compared on both its unit-file
  state and its active state

#### Scenario: A legacy scheduler baseline file is ignored

- **WHEN** the installer's state root holds a scheduler baseline file written
  by an earlier installer version, in any format
- **THEN** no action reads or rewrites it
- **AND** enable and rollback succeed or fail on the protected state captured
  by their own invocation

#### Scenario: Installing never disarms an armed lane

- **WHEN** the install action runs while the refresh timer or service reads
  back as enabled or active
- **THEN** it exits non-zero, tells the operator to roll back first, and issues
  no mutating systemd call
- **AND** it changes no unit file and no recorded baseline

#### Scenario: Install reports installed-stopped only when the lane reads back disarmed

- **WHEN** the install action has placed the unit files and reloaded systemd,
  and the refresh timer or service then reads back as enabled or active
- **THEN** the install exits non-zero, runs its restore handler, and does not
  report installed-stopped
- **AND** it reports installed-stopped only after the refresh timer and service
  each read back as neither enabled nor active, using the same disarmed
  definition as the refusal above

#### Scenario: A second install keeps the first install's restore baseline

- **WHEN** the install action runs on a disarmed lane whose restore baseline is
  already recorded
- **THEN** the baseline and its saved unit files are left byte-identical
- **AND** a later rollback restores the state and unit files that preceded the
  first install
- **AND** the baseline counts as recorded only once it has been written
  completely, so an interrupted first install is recaptured by the next one

#### Scenario: A malformed recorded state fails

- **WHEN** a recorded unit state has other than exactly two non-empty fields,
  or the recorded baseline has other than exactly one line per refresh unit
- **THEN** a rollback fails before its first mutation
- **AND** inside a restore handler the failure counts as a failed step, while
  the read-back still runs

### Requirement: A replace-uncertain refresh receipt describes the bytes on disk for every provider whose rollback was verified

When a refresh transaction's provider rollback is verified but the run must
still report `replace_uncertain`, because another lane's outcome is unknown, the
receipt SHALL describe the restored generation for each committed provider whose
rollback was verified. Its post-run digest, schema version, generation time and
payload checksum SHALL describe the bytes on disk when the receipt is written.
The receipt SHALL NOT describe the generation that was published and then rolled
back.

This SHALL NOT change the receipt schema. It SHALL NOT change any receipt whose
rollback was not verified, and it SHALL NOT change the outcome-based field
selection that the refresh-timer health probe applies to every receipt.

#### Scenario: A later lane's uncertainty does not leave restored providers claiming rolled-back bytes

- **WHEN** the registry, its worker mirror and the readiness index are
  published, a later lane's write leaves ownership unknowable, and the rollback
  of the three published providers is verified
- **THEN** the receipt's outcome is `replace_uncertain`
- **AND** for each of the three providers its post-run digest equals the digest
  of the bytes on disk, or is absent when the path is absent
- **AND** its post-run generation time is never newer than the manifest on disk
- **AND** the receipt, and any emergency record carrying it, still validates
  against the unchanged schema

#### Scenario: An unverified rollback keeps its evidence unchanged

- **WHEN** the provider rollback itself cannot be verified
- **THEN** the `replace_uncertain` receipt carries the committed evidence
  exactly as before this change
- **AND** readers keep choosing the registry generation time by outcome, because
  such a receipt may still describe bytes that are not on disk
