## ADDED Requirements

### Requirement: Database-backed candidate state

The scheduler SHALL persist candidate and stage state in database-backed pipeline/hydro/met records and events, not only local filesystem state files.

#### Scenario: terminal success skip

WHEN a scan finds an existing run for the same source/cycle/model/scenario in a terminal successful state
THEN it skips the candidate and records the terminal-state reason.

#### Scenario: hydro frequency terminal skip

WHEN a scan finds an existing hydro run in `frequency_done` or `published`
THEN the candidate is treated as terminal successful
AND native SHUD, parse, frequency, and publish stages are not resubmitted by default.

#### Scenario: active job skip

WHEN a scan finds a submitted or running Slurm job for the same candidate
THEN it checks current Slurm state
AND skips resubmission while the job remains active.

### Requirement: Resumable downstream failures

The scheduler SHALL resume from durable successful stage outputs instead of re-running expensive upstream stages unnecessarily.

#### Scenario: parse failed after SHUD success

WHEN SHUD output exists and the hydro run status indicates SHUD completed but parse or display publication failed
THEN retry starts from parse or publication
AND does not rerun native SHUD unless configured to force rerun.

#### Scenario: source unavailable retry policy

WHEN a source/cycle is unavailable
THEN the unavailable state is retryable according to configured source retry policy
AND it is distinguishable from model/runtime failure.

#### Scenario: transient array task retry

WHEN an array task fails with a transient Slurm/runtime classification such as node failure, preemption, timeout, or out-of-memory within retry limits
THEN retry targets the failed task or candidate scope rather than rerunning successful sibling tasks.

#### Scenario: permanent failure guard

WHEN a failure is classified as non-transient, malformed input, policy blocked, or over retry limit
THEN the candidate or task moves to permanent failure
AND automatic retry stops until an operator performs an explicit retry action.

#### Scenario: cancellation control

WHEN an operator cancels an active candidate, stage, or Slurm job
THEN the scheduler calls the Slurm cancellation contract where applicable
AND records cancelled status without submitting replacement work in the same pass.
