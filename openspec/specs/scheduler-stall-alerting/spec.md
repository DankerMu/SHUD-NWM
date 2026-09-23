# scheduler-stall-alerting Specification

## Purpose

A stalled compute lane must announce itself. This capability governs the node-22 side of
that: a read-only, database-free health probe that grades the DB-free scheduler's systemd
units, its governed pass artifacts and its no-progress tracker into one verdict per tick,
and exits non-zero for anything but healthy — the journal and a failed unit being the only
alerting channel that host has.

It is the node-22 counterpart of `frontier-stall-alerting`, which watches node-27's
published frontier. The two differ where their hosts differ: node-27 has a database and a
mail lane, node-22 has neither, so this capability carries no persistent state and no
outbound channel. Its detection-only boundary is load-bearing — the probe never starts,
stops or reconfigures the lane it watches.

## Requirements

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
sample alike while the copy silently grew permissive. That parity test SHALL be
routed in the repository's CI test selection so that it runs on a change to the
service-layer definition; a parity test the selector cannot reach on the very
diff that breaks parity is an after-the-fact detector, not a gate.

The verdicts SHALL be evaluated in a fixed precedence order with the first
matching condition winning: unreadable evidence, then the scheduler unit
signals, then the pass-artifact signals, then the no-progress tracker. The
healthy verdict SHALL be reachable only when no other condition matches, and
SHALL NOT be produced as a fallback for evidence the probe could not obtain.

Each non-healthy verdict SHALL name a runbook section, both in the receipt and
on the probe's error output, because a failed unit in the journal is this host's
only alerting channel. The receipt SHALL record every graded signal's observed
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

### Requirement: A pass that is running SHALL NOT be graded as a lane that has died

Liveness SHALL be taken from the scheduler units themselves, not inferred from
the age of the newest completed pass artifact. A healthy pass on this lane runs
for up to three and a quarter hours, so the newest completed artifact's age
conflates "the timer is dead" with "a long pass is in flight" and cannot carry
the liveness signal. The artifact-age signal SHALL remain as an independent
backstop for the case where the units are healthy but no artifact is being
produced, and its bound SHALL be derived from the measured inter-pass interval
with margin rather than from the timer's nominal cadence.

The scheduler service is a oneshot: while a pass runs it reports the activating
state and never the active state. Every verdict that is conditioned on the
service not running — the stopped-timer verdict, the not-triggering verdict and
the stale-evidence verdict — SHALL therefore treat both the active and the
activating state as running, and SHALL read the sub-state alongside the active
state. A verdict conditioned on equality with the active state alone would fire
during a healthy long pass, which is the exact failure this requirement exists
to prevent, and the stale-evidence verdict SHALL carry that condition
structurally rather than rely on its threshold happening to exceed the longest
observed pass.

The sub-state SHALL be tested against a named set of not-running values rather
than against inequality with the idle value: a oneshot whose run exited
non-zero reports a failed sub-state, and reading that as running would gate off
the stopped-timer verdict and report the lower-precedence service-failure
verdict in its place. An absent sub-state SHALL NOT be graded as either state:
a unit query that returns no sub-state is incomplete evidence and SHALL produce
the probe-failed verdict, which alarms at a higher precedence than any verdict
the sub-state would have gated.

The probe SHALL NOT grade a next-elapse signal, because this timer is armed
relative to its service becoming inactive and reports no realtime next elapse at
all. The timer itself stays active for the whole of a pass, so an inactive timer
SHALL be graded as stopped only when the service is also not running — a
conjunction that is conservative rather than a description of any reachable
in-flight geometry.

#### Scenario: A failed run is not mistaken for a running one

- **WHEN** the scheduler service reports a failed active state and a failed
  sub-state while the timer is inactive
- **THEN** the service counts as not running, so the stopped-timer verdict is
  reachable and is returned ahead of the lower-precedence service-failure
  verdict

#### Scenario: A long pass in flight is healthy

- **WHEN** the scheduler timer reports the active state with a running sub-state
  and the oneshot service reports the activating state with a start sub-state —
  the observed geometry of a pass in flight — while the last trigger time and
  the newest completed pass artifact are both older than their configured bounds
- **THEN** the probe does not return the stopped-timer verdict, does not return
  the not-triggering verdict, and does not return the stale-evidence verdict
- **AND** the remaining signals are still graded, so a genuine condition in the
  same tick still produces a non-healthy verdict

#### Scenario: Units healthy but artifacts stopped is its own verdict

- **WHEN** the timer is enabled and armed, the service's last result was
  success and the service is not running, but the newest completed pass
  artifact's recorded start time is older than the configured artifact-age bound
- **THEN** the probe returns the stale-evidence verdict and exits non-zero
- **AND** when no governed terminal pass artifact exists at all, the probe
  returns the distinct evidence-unavailable verdict rather than the stale one

### Requirement: The no-submission streak SHALL classify every pass shape it can meet, including the degraded ones

The sustained-no-submission signal SHALL classify each graded pass into exactly
one of: a pass that submitted work, which breaks the streak; a pass that
submitted nothing while holding at least one blocked candidate, which extends
it; a pass that submitted nothing and held no blocked candidate, which breaks it
because work that no longer appears blocked is no longer stalled; and a neutral
pass, which neither extends nor breaks it.

Neutral SHALL be decided by an observable the pass writer itself produces, not
by a list of pass status strings. The scheduler's own no-progress circuit
declares early-exit, pre-lock, lock-contended and resource-limit-aborted passes
neither-count-nor-clear, and those are four early-return sites rather than four
status values: the progress guard is constructed only after that region, so a
pass that returned earlier carries no progress-guard block at all. Neutral
SHALL therefore mean a pass whose artifact carries no progress-guard block,
whose submission or blocked-candidate count is absent, or whose status is the
resource-limit fallback. That last clause is not a return to a status allowlist
but a second reading of the same writer: the resource-limit path writes both
counts as zero and may still attach a progress-guard block, so a guard-tripped
resource-limit pass would otherwise read as idle and reset the streak — the
opposite of what the scheduler declares for that class.

A status allowlist SHALL NOT be used in its place, because it is wrong in both
directions on this lane: a reconciled-restart pass carrying dozens of blocked
candidates — the first pass of the very incident this probe exists for — would
be skipped as neutral, a preflight-blocked pass is fully observed and counted by
the scheduler yet would also be skipped, and an early-exit pass has no status
string of its own and would fall through to the idle case and reset the streak.

The absent-count case is real and is concentrated exactly where the probe is
needed most: the resource-limit fallback artifact is the shape that omits those
counts. Such a pass SHALL NOT be read as having submitted nothing, and SHALL NOT
by itself make the whole tick probe-failed, because a legitimate degraded
artifact inside the window would then suppress every other signal. The neutral
count SHALL be recorded in the receipt.

#### Scenario: A degraded artifact neither advances nor blocks the streak

- **WHEN** the graded window contains a resource-limit fallback artifact whose
  submission and blocked-candidate counts are absent, surrounded by passes that
  submitted nothing while holding blocked candidates
- **THEN** the degraded artifact does not advance the streak and does not reset
  it, the tick is not graded probe-failed on its account, and the receipt records
  it as neutral
- **AND** the resource-limit condition is still reported, because it is carried
  by its own higher-precedence verdict

#### Scenario: A fully observed pass is classified by its counts whatever its status

- **WHEN** the graded window contains a pass that carries a progress-guard
  block, reports a status other than the ordinary submitted or planned ones —
  a reconciled restart, or a preflight block — and records zero submissions with
  dozens of blocked candidates
- **THEN** that pass extends the streak as blocked work
- **AND** it is not treated as neutral, because the writer observed it fully

A neutral pass SHALL NOT consume a position in whatever bound the streak search
scans. Counting the streak inside a window whose size equals the alert threshold
makes the verdict unreachable as soon as one neutral pass falls inside it, which
on this lane is the common case rather than the exception. The search SHALL
instead run over the prefix the ordering margin can vouch for, stop at the first
pass that submitted work or held no blocked candidate, and report how many
neutral passes it skipped.

#### Scenario: A neutral pass does not cost the streak a position

- **WHEN** the configured streak length is reached only by passes that are not
  adjacent, because neutral passes are interleaved among them, under the shipped
  default thresholds
- **THEN** the submission-stalled verdict is returned
- **AND** the receipt records how many neutral passes the search skipped

#### Scenario: The streak needs sustained blocked work

- **WHEN** the configured number of consecutive graded passes each submitted
  nothing while holding at least one blocked candidate
- **THEN** the probe returns the submission-stalled verdict and exits non-zero
- **AND** one pass inside that window that submitted work, or that held no
  blocked candidate at all, breaks the streak and the verdict is not returned

#### Scenario: A transient degraded pass is reported on a window, not on being newest

- **WHEN** a pass carrying the resource-limit fallback status falls inside the
  configured lookback window but is no longer the newest pass, the window being
  evaluated over every artifact the probe successfully read rather than only
  over the shorter streak window
- **THEN** the probe returns the resource-limit verdict and exits non-zero,
  because that status holds only while its pass is newest — minutes on this lane
  — and a point check against the newest pass would almost never observe it
- **AND** the same pass falling outside the lookback window does not produce the
  verdict

#### Scenario: Repeated lock contention is its own verdict

- **WHEN** the configured number of consecutive graded passes each report the
  lock-contended status
- **THEN** the probe returns the lock-contended verdict and exits non-zero,
  a shape this lane has reached before and that no other signal covers: the
  artifacts stay fresh, no candidate is blocked, and the tracker does not grow

### Requirement: Recency SHALL be decided by the artifact's own recorded time, and exclusion SHALL precede truncation

The probe SHALL order the graded window by the start time each artifact records,
SHALL NOT consult file modification times, and SHALL bound both the number of
directory entries it considers and the number of artifacts it opens. This is
load-bearing because the governed pass artifact names carry the cycle only to
the hour followed by a random suffix, so their lexical order contradicts the
order of the start times they record within any hour.

Pre-execution snapshots SHALL be excluded from the candidate set **before** the
lexical truncation that bounds it, not after: a pre-execution snapshot sorts
after the terminal artifact of the same pass, the two coexist on disk long-term,
and truncating first would keep the snapshot and discard the terminal artifact
at the boundary, defeating the margin that makes the bounded scan sound.

#### Scenario: Lexical order contradicts recorded order

- **WHEN** the evidence root holds governed artifacts whose lexical order
  contradicts the order of their recorded start times
- **THEN** the probe grades the artifact whose recorded start time is newest
- **AND** rewriting an old artifact's file modification time to the present
  changes no verdict

#### Scenario: A pre-execution snapshot does not displace its terminal artifact

- **WHEN** a pass's terminal artifact and its pre-execution snapshot both sit at
  the boundary of the bounded candidate scan
- **THEN** the terminal artifact enters the graded window
- **AND** the scan considers enough names that the arbitrary intra-hour
  selection at that boundary cannot displace a pass that belongs in the window;
  a configuration that does not guarantee this is refused before any artifact is
  opened

### Requirement: A structurally unconvergeable no-progress subject SHALL be suppressible by declared reason without suppressing anything else

The no-progress signal SHALL admit a declared set of suppressed reason strings,
matched exactly, whose entries are excluded from that verdict. The suppression
SHALL be stateless configuration whose shipped default is tracked in the
repository: the probe SHALL NOT decide on its own which subjects to ignore by
comparing against its own previous output, because a verdict that clears itself
on the next tick is the probe acknowledging its own alert, and a non-zero unit
must persist while the condition persists. Matching SHALL be on the reason and
not on the subject, because tracker subjects carry a cycle and a subject-keyed
list would need re-editing every cycle.

Suppression SHALL apply to the no-progress verdict alone and SHALL NOT affect
any other signal's grading. Every suppressed entry SHALL still be recorded in
the receipt with its subject, reason, consecutive-pass count, and the rule that
matched it. The operator documentation SHALL state, with its source, why each
shipped suppressed reason cannot converge.

#### Scenario: A chronic entry does not mask a new one

- **WHEN** the tracker holds a suppressed chronic entry at a very high
  consecutive-pass count alongside an unsuppressed entry at or above the
  threshold
- **THEN** the probe returns the no-progress verdict and exits non-zero

#### Scenario: Only chronic entries grade healthy but stay visible

- **WHEN** every tracker entry at or above the threshold matches a declared
  suppressed reason, and no other signal matches
- **THEN** the probe returns the healthy verdict and exits zero
- **AND** the receipt lists each suppressed entry with its consecutive-pass
  count and the matching rule

#### Scenario: Suppression never reaches the other signals

- **WHEN** a declared suppressed reason is present in the tracker while a
  scheduler unit signal, the stale-evidence signal, the resource-limit signal,
  the lock-contention signal, or the no-submission streak also matches
- **THEN** the corresponding verdict is returned unchanged and the exit status
  is non-zero

### Requirement: The watched scheduler units SHALL NOT be touched by the watcher

The probe's own systemd units SHALL query the scheduler units and never depend
on them: no ordering or requirement directive naming the scheduler timer or
service SHALL appear in the probe's unit files, so the watcher can never drag
the watched lane into its own start-up or failure path.

The probe service SHALL carry no environment file. Its thresholds and paths are
environment-overridable, and the shipped defaults are the production values; a
retune SHALL be a recorded drop-in rather than an untracked file that can
silently relax an alert threshold. The unit SHALL unset the database
environment variables it must never see, because this host connects to no
database and the probe requires none.

This change ships no installer. The precedent probe's installer enforces its
protected-state guarantee and rolls back on failure across four units; the
manual before-and-after comparison documented here is weaker on purpose, and the
documentation SHALL say what is given up — enforcement, automatic rollback, and
the coverage of the file-provider refresh units — rather than claim equivalence.

#### Scenario: The probe's units declare no relationship to the scheduler

- **WHEN** the probe's service and timer unit files are read
- **THEN** neither names the scheduler timer or service in any ordering or
  dependency directive
- **AND** neither carries an environment file, and the service unsets the
  database environment variables

#### Scenario: Installation does not disturb the scheduler

- **WHEN** an operator installs the probe by the documented procedure
- **THEN** the scheduler timer's and service's unit-file state and active state
  are recorded before and after and are unchanged
- **AND** the documentation states that this manual comparison replaces an
  enforced, self-rolling-back installer and names what that costs
