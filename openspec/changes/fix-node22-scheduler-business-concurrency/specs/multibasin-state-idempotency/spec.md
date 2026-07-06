## MODIFIED Requirements

### Requirement: Database-backed candidate state

The scheduler SHALL persist candidate and stage state in database-backed or
DB-free file-backed pipeline/hydro/met records and events, not only local
filesystem state files. Candidate selection SHALL treat the strongest terminal
cycle/stage/copyback evidence as authoritative for skip decisions when weaker
legacy rows are stale.

#### Scenario: terminal success skip

WHEN a scan finds an existing pipeline job, forecast cycle, stage/copyback
record, or hydro run for the same source/cycle/model/scenario in a terminal
successful state
THEN it skips the candidate and records the terminal-state reason.

#### Scenario: hydro durable terminal skip

WHEN a scan finds an existing hydro run in `succeeded`, `parsed`, or `published`
THEN the candidate is treated as terminal successful
AND native SHUD, parse, publish, Slurm submission, and orchestrator execution are
not resubmitted by default
AND the skip evidence records the durable hydro status that caused the skip.

#### Scenario: completed cycle overrides stale hydro created row

WHEN a scan finds the forecast cycle complete and terminal stage/copyback
evidence for a candidate but an older hydro row still has status `created`
THEN the candidate is treated as terminal successful
AND the scheduler records the stale-row evidence
AND it MUST NOT submit forcing, forecast, parse, state-save, or copyback work for
that candidate by default.

#### Scenario: active job skip

WHEN a scan finds a submitted or running Slurm job for the same candidate
THEN it checks current Slurm state
AND skips resubmission while the job remains active.

### Requirement: Resumable downstream failures

The scheduler SHALL resume from durable successful stage outputs instead of
re-running expensive upstream stages unnecessarily. Before resuming downstream
stages, it SHALL verify that required upstream artifacts referenced by the
candidate state still exist in the configured object-store/copyback roots.

#### Scenario: parse failed after SHUD success

WHEN SHUD output exists and the hydro run status indicates SHUD completed but
parse or display publication failed
THEN retry starts from parse or publication
AND does not rerun native SHUD unless configured to force rerun.

#### Scenario: missing forcing package blocks downstream forecast resume

WHEN historical state says forcing or forecast succeeded but the
`forcing_package_uri` tree required by a downstream forecast resume is missing
from the configured object-store/copyback root
THEN the candidate is blocked with stable artifact recovery evidence or
restarted from the upstream forcing stage according to policy
AND the scheduler MUST NOT submit the forecast stage directly
AND the error MUST NOT be collapsed into generic `NODE_FAILURE`.

#### Scenario: source unavailable retry policy

WHEN a source/cycle is unavailable
THEN the unavailable state is retryable according to configured source retry
policy
AND it is distinguishable from adapter, model, forcing, SHUD/runtime, parse, and
publication failures
AND retry evidence records a classifier, reason code, attempt count, retry
limit, and enum-safe storage location without writing unsupported database enum
states.

#### Scenario: transient array task retry

WHEN an array task fails with a transient Slurm/runtime classification such as
node failure, preemption, timeout, or out-of-memory within retry limits
THEN retry targets the failed task or candidate scope rather than rerunning
successful sibling tasks
AND persisted/evidence fields record the failure classifier, retry attempt,
retry limit, stage/task identity, and reused successful sibling outputs.

#### Scenario: permanent failure guard

WHEN a failure is classified as non-transient, malformed input, policy blocked,
or over retry limit
THEN the candidate or task moves to permanent failure
AND automatic retry stops until an operator performs an explicit retry action
AND pipeline events or scheduler evidence preserve the classifier, reason code,
prior attempt count, retry limit, and permanent-failure decision.

#### Scenario: manual retry after permanent or blocked state

WHEN an operator performs an explicit retry for a candidate or task previously
marked permanent, blocked, or retry-limit-exhausted
THEN the retry is allowed only with a manual retry marker
AND the new attempt records incremented attempt evidence, the manual retry
marker, and the prior failure reason for auditability.

#### Scenario: cancellation control

WHEN an operator cancels an active candidate, stage, or Slurm job
THEN the scheduler calls the Slurm cancellation contract where applicable
AND records cancelled status without submitting replacement work in the same
pass.

#### Scenario: cancellation proof gap

WHEN the Slurm cancellation contract is unavailable, returns an error, or does
not prove the job reached a terminal cancelled state
THEN the scheduler records cancellation proof-gap evidence in
`ops.pipeline_event.details` or scheduler evidence
AND preserves local job state instead of fabricating cancellation success
AND does not submit replacement work in the same scheduler pass.
