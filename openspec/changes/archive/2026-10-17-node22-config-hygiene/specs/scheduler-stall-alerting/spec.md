# Spec Delta

## MODIFIED Requirements

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
be skipped as neutral, a post-candidate preflight-blocked pass (`evidence_preflight_blocked`,
`slurm_preflight_blocked`, bare `preflight_blocked`) is fully observed and counted by
the scheduler yet would also be skipped, and an early-exit pass has no status
string of its own and would fall through to the idle case and reset the streak.

A pre-lock root-blocked pass (`execution_boundary` equal to
`scheduler_root_preflight_blocked`) carries no progress-guard block and is therefore
neutral under the rule above, for this streak, for the in-flight time gate and for
frontier selection; it SHALL NOT be re-classified as blocked or idle, so the shape
taxonomy stays closed and the receipt shape counts are unchanged. Its condition is
reported by the separate root-blocked run predicate (next requirement), which reads
`execution_boundary` directly instead of the counts.

The absent-count case is real and is concentrated exactly where the probe is
needed most: the resource-limit fallback artifact is the shape that omits those
counts. Such a pass SHALL NOT be read as having submitted nothing, and SHALL NOT
by itself make the whole tick probe-failed, because a legitimate degraded
artifact inside the window would then suppress every other signal. The neutral
count SHALL be recorded in the receipt.

#### Scenario: A degraded artifact neither advances nor blocks the streak

- **WHEN** the graded window contains a resource-limit fallback artifact whose submission and blocked-candidate counts are absent, surrounded by passes that submitted nothing while holding blocked candidates
- **THEN** the degraded artifact does not advance the streak and does not reset it, the tick is not graded probe-failed on its account, and the receipt records it as neutral
- **AND** the resource-limit condition is still reported, because it is carried by its own higher-precedence verdict

#### Scenario: A fully observed pass is classified by its counts whatever its status

- **WHEN** the graded window contains a pass that carries a progress-guard block, reports a status other than the ordinary submitted or planned ones (a reconciled restart, or a post-candidate preflight block) and records zero submissions with dozens of blocked candidates
- **THEN** that pass extends the streak as blocked work
- **AND** it is not treated as neutral, because the writer observed it fully

#### Scenario: A root-blocked pass stays neutral for every existing signal

- **WHEN** a pass with `execution_boundary: scheduler_root_preflight_blocked` and no progress-guard block sits between two passes that count toward the no-submission streak, or inside an in-flight-held run, or is the newest pass while a frontier entry is bypassed
- **THEN** the streak, the in-flight span and the frontier selection are the same as if that pass were absent, and the receipt counts it as neutral

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

## ADDED Requirements

### Requirement: A root-blocked scheduler SHALL become a non-zero unit

The probe SHALL read `execution_boundary` from each pass artifact into the pass record. A root-blocked run SHALL be the contiguous newest-first sequence of passes whose `execution_boundary` equals exactly `scheduler_root_preflight_blocked`; any other pass ends the run. When the newest pass is in the run and the minutes from the oldest run member's recorded time to the newest are greater than or equal to `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES` (default 60), the verdict SHALL be `scheduler_root_blocked`, exit status 1, runbook section `stall-scheduler-root-blocked`, graded immediately after `lock_contended_persistent` and before `submission_stalled`. Configuration SHALL reject the threshold when it is below twice the minimum inter-pass interval or above the same reachable bound the in-flight gate uses. The receipt SHALL record `root_blocked_run` with `passes`, `span_minutes`, `threshold_minutes` and `newest_is_root_blocked` so the verdict can be re-derived from the receipt alone.

#### Scenario: One hour of root-blocked passes

- **WHEN** the newest thirteen passes carry `execution_boundary: scheduler_root_preflight_blocked`, no progress-guard block and zero counts, with recorded times five minutes apart so the span is exactly 60 minutes, and no higher-precedence condition holds
- **THEN** the verdict is `scheduler_root_blocked`, the receipt names `stall-scheduler-root-blocked` and `root_blocked_run.span_minutes` is 60, and the exit status is 1

#### Scenario: A single transient refusal stays ok

- **WHEN** only the newest pass is root-blocked and the pass before it submitted normally within the window
- **THEN** the verdict is `ok` and `root_blocked_run.passes` is 1

#### Scenario: A non-root pass ends the run

- **WHEN** a `db_free_runtime_preflight_blocked` pass sits between two root-blocked groups
- **THEN** the run counts only the newer group

#### Scenario: Dead lane and pass-limit outrank blocked root

- **WHEN** the scheduler timer is stopped, or a resource-limit pass is inside the pass-limit lookback, while the newest passes are root-blocked beyond the threshold
- **THEN** the verdict is `timer_stopped` or `pass_limit_blocked` respectively, and the receipt still records the root-blocked run

#### Scenario: Blocked root outranks a stalled submission streak

- **WHEN** the root-blocked run meets its threshold and the no-submission streak also meets its threshold
- **THEN** the verdict is `scheduler_root_blocked`

#### Scenario: Unreachable or trivial threshold is rejected

- **WHEN** `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES` is 5 or exceeds the reachable bound
- **THEN** configuration loading fails with a message naming the variable and the allowed range
