## MODIFIED Requirements

### Requirement: A stalled DB-free scheduler SHALL become a non-zero unit without anyone reading an artifact

The system SHALL provide a read-only, database-free health probe for the node-22
DB-free scheduler lane that reads the scheduler timer and service state, the
governed pass artifacts under the configured evidence root, and the no-progress
tracker, grades them into exactly one verdict, writes a bounded receipt, and
exits non-zero for every verdict other than healthy. The probe SHALL be
detection only: it SHALL NOT start, stop, enable, disable, restart, or reload
any systemd unit, SHALL NOT write anything under the evidence root, and SHALL
NOT clear any lock.

The probe SHALL be self-contained standard-library code that imports no
repository package, so it can be staged and run from outside the deployed
checkout — checking a feature branch out in that tree would put unreviewed code
into the live scheduler tick. Any contract it therefore has to carry a second
copy of — the governed pass filename predicate, its prefix and accepted
suffixes, and the writer's maximum evidence size — SHALL be pinned to the
service-layer definitions by a parity test that asserts those constants
themselves and not merely that a sample of names is classified alike, because a
predicate later narrowed to a stricter name shape would still classify any fixed
sample alike while the copy silently grew permissive.

The verdicts SHALL be evaluated in a fixed precedence order with the first
matching condition winning: unreadable evidence, then the scheduler unit
signals, then the pass-artifact signals, then the no-progress tracker. The
healthy verdict SHALL be reachable only when no other condition matches, and
SHALL NOT be produced as a fallback for evidence the probe could not obtain.

Each non-healthy verdict SHALL name a runbook section, both in the receipt and
on the probe's error output, because the failed unit is what the host's
`OnFailure=` alert lane forwards (`nhms-node22-unit-failure-alert@.service`,
with the unit's last journal lines). The receipt SHALL record every graded signal's observed
value beside the threshold it was compared against, so the verdict can be
re-derived from the receipt alone, and the receipt SHALL be written outside the
evidence root so the probe's own output never enters readiness discovery or
pass-evidence retention.

#### Scenario: The scheduler unit signals outrank the artifact signals

- **WHEN** the scheduler timer is not enabled, or is inactive while its service
  is also not running, or the service's last result was not success, or the last
  trigger is older than the configured bound while the service is not running —
  and the newest pass artifact simultaneously carries the resource-limit
  fallback status
- **THEN** the probe returns the corresponding unit verdict, not the artifact
  verdict, and exits non-zero, because once the lane has stopped running the
  artifact's statement describes a moment in the past
- **AND** the receipt records both observations, so the outranked one stays
  readable

#### Scenario: Missing or untrustworthy evidence is never graded healthy

- **WHEN** the systemd query cannot be executed, exits non-zero, or returns
  fields the probe cannot parse; or the evidence root cannot be listed, or holds
  more entries than the probe's bounded enumeration admits — an unordered
  truncation would silently invalidate the margin the bounded candidate scan
  relies on, so reaching that bound is a reported fact rather than a default; or a
  governed artifact in the graded window is a symlink, is not a regular file,
  exceeds the writer's own maximum evidence size, fails to parse, or records no
  start time; or the tracker is a symlink, is not a regular file, exceeds the
  read bound, fails to parse, carries an unrecognized schema version, or holds a
  malformed entry
- **THEN** the probe returns the probe-failed verdict and exits non-zero
- **AND** the healthy verdict is not reachable on that tick
- **AND** a tracker file that is simply absent is a determinate observation of
  zero entries rather than a failure, because the scheduler writes no tracker at
  all when its circuit is disabled and a missing tracker is the documented shape
  of a first enablement; the receipt records that it was absent

#### Scenario: Configuration is refused before any evidence is gathered

- **WHEN** a threshold falls outside its declared range, the candidate scan
  limit does not exceed the longest graded streak by more than one hour of
  passes (so the ordering-safe prefix the streak search walks would be no
  longer than the threshold, and one neutral pass inside it would make the
  verdict unreachable), or the evidence root is not a directory
- **THEN** the probe exits with the configuration-refusal status, distinct from
  the alert status
- **AND** no pass artifact is opened and no systemd query is executed

#### Scenario: Non-pass state in the evidence root is classified as the service layer classifies it

- **WHEN** the evidence root contains the permanent no-progress tracker,
  operator repair and stale-lock-clear receipts, a retention subdirectory, and
  pre-execution pass snapshots beside the governed terminal pass artifacts
- **THEN** only governed terminal pass artifacts enter the graded window
- **AND** the probe's own copy of the filename predicate and of the maximum
  evidence size agree with the service-layer definitions on every one of those
  names and values, asserted by a test that imports both
