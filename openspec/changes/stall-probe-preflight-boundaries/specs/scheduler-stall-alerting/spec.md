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

A guard-less pre-candidate preflight-blocked pass (`execution_boundary` equal to
`scheduler_root_preflight_blocked`, `db_free_runtime_preflight_blocked`,
`scheduler_rollback_fence_prepared` or `db_free_registry_blocked`) carries no
progress-guard block and is therefore neutral under the rule above, for this streak,
for the in-flight time gate and for frontier selection; it SHALL NOT be re-classified
as blocked or idle, so the shape taxonomy stays closed and the receipt shape counts
are unchanged. Its condition is reported by the separate preflight-blocked run
predicate (the root-blocked and guard-less preflight-blocked requirements), which
reads `execution_boundary` directly instead of the counts.

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

### Requirement: A root-blocked scheduler SHALL become a non-zero unit

The probe SHALL read `execution_boundary` from each pass artifact into the pass record. The root-blocked verdict SHALL be graded on the preflight-blocked run defined by the next requirement: when that run's newest pass has `execution_boundary` exactly `scheduler_root_preflight_blocked` and the run's span is greater than or equal to `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES` (default 60), the verdict SHALL be `scheduler_root_blocked`, exit status 1, runbook section `stall-scheduler-root-blocked`, graded in slot 10, immediately after `lock_contended_persistent` and before `submission_stalled`. Configuration SHALL reject the threshold when it is below twice the minimum inter-pass interval or above the same reachable bound the in-flight gate uses.

The receipt SHALL keep recording `root_blocked_run` with `passes`, `span_minutes`, `threshold_minutes` and `newest_is_root_blocked`, measured exactly as before over the contiguous newest-first sequence of passes whose `execution_boundary` equals exactly `scheduler_root_preflight_blocked`, any other pass ending it. That root-exact run is a prefix of the graded run, so `newest_is_root_blocked` with `span_minutes >= threshold_minutes` SHALL remain sufficient for the verdict; the verdict SHALL be re-derivable from the receipt's `preflight_blocked_run` block alone.

#### Scenario: One hour of root-blocked passes

- **WHEN** the newest thirteen passes carry `execution_boundary: scheduler_root_preflight_blocked`, no progress-guard block and zero counts, with recorded times five minutes apart so the span is exactly 60 minutes, and no higher-precedence condition holds
- **THEN** the verdict is `scheduler_root_blocked`, the receipt names `stall-scheduler-root-blocked` and `root_blocked_run.span_minutes` is 60, and the exit status is 1

#### Scenario: A single transient refusal stays ok

- **WHEN** only the newest pass is root-blocked and the pass before it submitted normally within the window
- **THEN** the verdict is `ok` and `root_blocked_run.passes` is 1

#### Scenario: A non-root pass ends the run

- **WHEN** a `db_free_runtime_preflight_blocked` pass sits between two root-blocked groups and the three groups together span the threshold while the newer root group alone does not
- **THEN** `root_blocked_run` counts only the newer group, `preflight_blocked_run` counts all of them, and the verdict is `scheduler_root_blocked`
- **AND** when the splitter is instead `scheduler_timing_level_unrecognised` or a pass with no `execution_boundary`, both runs count only the newer group and the verdict is `ok`

#### Scenario: Dead lane and pass-limit outrank blocked root

- **WHEN** the scheduler timer is stopped, or a resource-limit pass is inside the pass-limit lookback, while the newest passes are root-blocked beyond the threshold
- **THEN** the verdict is `timer_stopped` or `pass_limit_blocked` respectively, and the receipt still records the root-blocked run

#### Scenario: Blocked root outranks a stalled submission streak

- **WHEN** the root-blocked run meets its threshold and the no-submission streak also meets its threshold
- **THEN** the verdict is `scheduler_root_blocked`

#### Scenario: Unreachable or trivial threshold is rejected

- **WHEN** `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES` is 5 or exceeds the reachable bound
- **THEN** configuration loading fails with a message naming the variable and the allowed range

## ADDED Requirements

### Requirement: Every guard-less preflight-blocked boundary SHALL become a non-zero unit naming its remedy

The probe SHALL carry a pinned table of the guard-less pre-candidate `preflight_blocked` boundaries whose artifact lands, each mapped to the verdict of its remedy class: `scheduler_root_preflight_blocked` to `scheduler_root_blocked`, `db_free_runtime_preflight_blocked` to `scheduler_db_free_runtime_blocked`, `scheduler_rollback_fence_prepared` to `scheduler_rollback_fenced`, and `db_free_registry_blocked` to `scheduler_registry_blocked`; the runbook sections are `stall-scheduler-root-blocked`, `stall-scheduler-db-free-runtime-blocked`, `stall-scheduler-rollback-fenced` and `stall-scheduler-registry-blocked`, one per verdict.

The preflight-blocked run SHALL be the contiguous newest-first sequence, within the ordering-safe prefix, of passes whose `execution_boundary` equals exactly any key of that table; a change from one table boundary to another SHALL NOT end the run, because every member is a pass the scheduler refused before candidate selection, so the lane has submitted nothing across the whole span. Any other pass SHALL end it, including `scheduler_timing_level_unrecognised`, `scheduler_progress_guard_blocked`, a pass with no `execution_boundary`, and every pass that reached candidate selection. When the newest pass is in the run and the run's span is greater than or equal to `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES`, the verdict SHALL be the table verdict of the newest pass's boundary, exit status 1, in slot 10; the four verdicts are mutually exclusive because they are picked by the same newest pass. These passes SHALL stay neutral for the no-submission streak, the in-flight time gate and frontier selection.

The receipt SHALL record `preflight_blocked_run` on every verdict with `passes`, `span_minutes`, `threshold_minutes`, `newest_boundary`, `remedy_verdict` (the table verdict of `newest_boundary`, or null for an empty run) and `boundary_passes` (the number of run members per boundary), so slot 10 can be re-derived from the receipt alone: it fires `remedy_verdict` when that is not null and `span_minutes >= threshold_minutes`.

A parity test SHALL enumerate, from the writer source, every dict literal whose `status` is `preflight_blocked`, and SHALL assert that the boundaries of those without a `progress_guard` key equal the probe's table plus an explicit exemption set (`scheduler_timing_level_unrecognised`, whose artifact never lands), and that those with one equal the explicit guarded set (`scheduler_progress_guard_blocked`); a literal whose boundary cannot be read SHALL fail it. The repository's CI test selection SHALL route a change to the writer module to that test.

#### Scenario: One hour of a sibling boundary raises its own verdict

- **WHEN** the newest thirteen passes, five minutes apart, all carry one of `db_free_runtime_preflight_blocked`, `scheduler_rollback_fence_prepared` or `db_free_registry_blocked`, no progress-guard block and zero counts, and no higher-precedence condition holds
- **THEN** the verdict is respectively `scheduler_db_free_runtime_blocked`, `scheduler_rollback_fenced` or `scheduler_registry_blocked`, the exit status is 1, the receipt names that verdict's runbook section, and `preflight_blocked_run` records 13 passes over 60 minutes with that boundary as `newest_boundary`
- **AND** twelve such passes span 55 minutes and the verdict is `ok`

#### Scenario: Repairing the roots while the NFS is unmounted never returns to ok

- **WHEN** a lane that has been `scheduler_root_preflight_blocked` for an hour flips to `db_free_runtime_preflight_blocked` because the `/scratch` roots were repaired while `/ghdc` stays unmounted
- **THEN** from the first tick after the flip the verdict is `scheduler_db_free_runtime_blocked`, not `ok`, and `preflight_blocked_run.boundary_passes` counts both boundaries
- **AND** after one pass that submits work the verdict is `ok` and `preflight_blocked_run.passes` is 0

#### Scenario: A flip back to the root boundary keeps the run

- **WHEN** an hour of `db_free_runtime_preflight_blocked` passes is followed by one `scheduler_root_preflight_blocked` pass
- **THEN** the verdict is `scheduler_root_blocked` although `root_blocked_run.span_minutes` is 0

#### Scenario: Higher slots outrank and the streak is outranked

- **WHEN** a sibling run meets the threshold while the timer is stopped, or while a resource-limit pass is inside the lookback, or while the no-submission streak also meets its threshold
- **THEN** the verdict is `timer_stopped`, `pass_limit_blocked`, or the sibling's own verdict respectively, and the receipt still records the run

#### Scenario: A new guard-less boundary in the writer turns the parity test red

- **WHEN** the writer source gains a `preflight_blocked` literal whose `execution_boundary` is neither in the table nor exempt, or a guarded literal outside the guarded set, or a literal whose boundary is not a string constant
- **THEN** the parity test fails

#### Scenario: The receipt block is written on a verdict with no records

- **WHEN** the tick is graded `probe_failed`
- **THEN** `preflight_blocked_run` is present with zero passes, null span, null `newest_boundary`, null `remedy_verdict` and empty `boundary_passes`
