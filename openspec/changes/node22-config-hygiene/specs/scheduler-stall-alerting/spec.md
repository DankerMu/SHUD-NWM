# Spec Delta

## MODIFIED Requirements

### Requirement: The no-submission streak SHALL classify every pass shape it can meet, including the degraded ones

The sustained-no-submission signal SHALL classify each graded pass into exactly one of: a pass that submitted work, which breaks the streak; a pass that submitted nothing while holding at least one blocked candidate, which extends it; a pass that submitted nothing and held no blocked candidate, which breaks it; and a neutral pass, which neither extends nor breaks it.

Neutral SHALL be decided by an observable the pass writer itself produces, not by a list of pass status strings: a pass whose artifact carries no progress-guard block, whose submission or blocked-candidate count is absent, or whose status is the resource-limit fallback. A pre-lock root-blocked pass (`execution_boundary == "scheduler_root_preflight_blocked"`) carries no progress-guard block and is therefore neutral for this streak, for the in-flight time gate and for frontier selection; it SHALL NOT be re-classified as blocked or idle. Its condition is reported by the separate root-blocked run predicate below, so the shape taxonomy stays closed and the receipt `shapes` counts are unchanged. Post-candidate preflight blocks (`evidence_preflight_blocked`, `slurm_preflight_blocked`, bare `preflight_blocked`) carry a progress guard and SHALL keep being classified by their counts.

A status allowlist SHALL NOT be used in its place. The neutral count SHALL be recorded in the receipt.

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
