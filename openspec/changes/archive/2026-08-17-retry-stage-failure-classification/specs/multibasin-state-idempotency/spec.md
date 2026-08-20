# multibasin-state-idempotency delta

## MODIFIED Requirements

### Requirement: Resumable downstream failures

The scheduler SHALL resume from durable successful stage outputs instead of re-running expensive upstream stages unnecessarily, subject to the permanence judgement of `job-retry-mechanism`: for a downstream failure whose genuinely recorded error code — one recorded by the failing stage itself: its own failed job row, or the candidate/run-level failure fields; stale codes left by recovered stages or by retry-history keys (`last_error`, `previous_error`) are not evidence for this judgement — classifies as permanent, defaults non-transient under the unknown-code clause, or has exhausted its retry budget, the downstream-resume channel refuses to resume, and absent another legitimate channel claim (e.g. a genuinely changed model package) the candidate falls to the permanent-failure guard.

#### Scenario: parse failed after SHUD success

WHEN SHUD output exists and the hydro run status indicates SHUD completed but parse or display publication failed with a recorded transient error code within budget, or with no recorded error code (the reader-synthesized placeholder domain, outside its refused classifiers), and the state does not explicitly mark the failure permanent (top-level `permanent: true`)
THEN retry starts from parse or publication
AND does not rerun native SHUD unless configured to force rerun.

#### Scenario: recorded non-transient downstream failure is guarded, not resumed

WHEN SHUD output exists but the downstream failure carries a genuinely recorded error code that is non-transient (e.g. `OUTPUT_INCOMPLETE` or a recorded `PARSE_FAILED`, classified non-transient since the stage-failure family joined the non-transient list), unknown and defaulted non-transient (e.g. a recorded `SLURM_JOB_FAILED`), or over its retry budget
THEN the downstream-resume channel refuses to resume
AND absent another legitimate channel claim (e.g. a genuinely changed model package under `job-retry-mechanism`) the candidate moves to the permanent-failure guard with automatic retry refused, where resumption requires an explicit operator retry action, consistent with the permanent failure guard scenario of this spec

#### Scenario: stale historical codes do not flip the resume domain

WHEN SHUD output exists and the current downstream failure records no error code of its own, while the state still carries a stale code elsewhere — a recovered (succeeded) stage's leftover `error_code`, or a retry-history key such as an auto-retry event's `previous_error`
THEN the downstream-resume judgement treats the failure as the reader-synthesized placeholder domain (the stale code is not evidence for the domain split)
AND the resume is then decided by the placeholder domain's existing classifier rules — the stale code still reaches the classifier through the reason-code text surface, so a stale code classifying into that domain's refused classifiers (e.g. `OUT_OF_MEMORY` → resource configuration) keeps its existing refusal (unchanged `job-retry-mechanism` semantics), while codes outside them resume as they would with no stale code present

#### Scenario: source unavailable retry policy

WHEN a source/cycle is unavailable
THEN the unavailable state is retryable according to configured source retry policy
AND it is distinguishable from adapter, model, forcing, SHUD/runtime, parse, and publication failures
AND retry evidence records a classifier, reason code, attempt count, retry limit, and enum-safe storage location without writing unsupported database enum states.

#### Scenario: transient array task retry

WHEN an array task fails with a transient Slurm/runtime classification such as node failure, preemption, or timeout within retry limits (`OUT_OF_MEMORY` is NOT transient: per `job-retry-mechanism`'s Retry Guard — Non-Transient Error Exclusion it is a configuration error that takes the permanent-failure path with automatic retry refused)
THEN retry targets the failed task or candidate scope rather than rerunning successful sibling tasks
AND persisted/evidence fields record the failure classifier, retry attempt, retry limit, stage/task identity, and reused successful sibling outputs.

#### Scenario: permanent failure guard

WHEN a failure is classified as non-transient, malformed input, policy blocked, or over retry limit
THEN the candidate or task moves to permanent failure
AND automatic retry stops until an operator performs an explicit retry action
AND pipeline events or scheduler evidence preserve the classifier, reason code, prior attempt count, retry limit, and permanent-failure decision.

#### Scenario: manual retry after permanent or blocked state

WHEN an operator performs an explicit retry for a candidate or task previously marked permanent, blocked, or retry-limit-exhausted
THEN the retry is allowed only with a manual retry marker
AND the new attempt records incremented attempt evidence, the manual retry marker, and the prior failure reason for auditability.

#### Scenario: cancellation control

WHEN an operator cancels an active candidate, stage, or Slurm job
THEN the scheduler calls the Slurm cancellation contract where applicable
AND records cancelled status without submitting replacement work in the same pass.

#### Scenario: cancellation proof gap

WHEN the Slurm cancellation contract is unavailable, returns an error, or does not prove the job reached a terminal cancelled state
THEN the scheduler records cancellation proof-gap evidence in `ops.pipeline_event.details` or scheduler evidence
AND preserves local job state instead of fabricating cancellation success
AND does not submit replacement work in the same scheduler pass.
