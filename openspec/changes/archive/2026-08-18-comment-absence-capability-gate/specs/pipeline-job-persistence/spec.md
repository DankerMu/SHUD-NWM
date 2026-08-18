## ADDED Requirements

### Requirement: Comment-based absence proof requires proven comment accounting capability

The restart-reconciliation comment querier SHALL refuse to answer — raising
its transient query-unavailable error with reason class
`comment_accounting_unproven` before issuing any sacct command — unless a
once-per-querier-instance probe of `scontrol show config` proves that
`AccountingStoreFlags` includes the `job_comment` flag, because on a
cluster whose accounting never stores the sbatch comment a comment search
can never find a genuinely in-flight job, so treating its empty answer as
a confirmed absence falsely demotes live reservations to
`reservation_lost` and re-submits their cohorts. A probe that cannot run,
a missing `AccountingStoreFlags` line, and a `(null)` or
`job_comment`-less flag value all count as unproven (fail-closed toward
refusing, never toward trusting the search), with a warning that
distinguishes probe-execution failure from a cluster whose flags provably
lack `job_comment`. Refusal keeps the existing transient-deny semantics:
reserved rows stay reserved past the grace window and no absence
conclusion is recorded. This outcome class deliberately does not
converge on its own: it does not increment the identity-mismatch streak
counter (whose convergence requirement covers only the
`identity_mismatch_blocked` outcome family), adds no automatic release
exit, and leaves disposition to the documented runbook procedure (which
today may terminate in escalation rather than repair) — on
such clusters no reliable automatic absence proof exists, so any
automatic exit would trade duplicate submission against abandoning a
live job. On clusters where the probe proves the capability, sacct query
behavior is unchanged and the querier's raise-priority order is
preserved: the accepted-submit contract-version check still raises first,
and the global-visibility gate still applies to the queries it guarded
before.

#### Scenario: a cluster that does not store comments never confirms absence

WHEN the probe reads `AccountingStoreFlags = (null)` (or the flag list
lacks `job_comment`, or the line is absent, or scontrol fails)
THEN every comment query — owner-scoped, global, and legacy — raises the
transient query-unavailable error with reason class
`comment_accounting_unproven` without invoking sacct, and a reserved row
past its grace window stays reserved instead of being demoted to
`reservation_lost`

#### Scenario: a comment-storing cluster is unchanged

WHEN the probe proves `AccountingStoreFlags` includes `job_comment`
THEN owner-scope and global-scope comment queries page sacct exactly as
before, owned matches still bind, and a coverage-complete confirmed
absence older than the grace window still demotes to `reservation_lost`

#### Scenario: the probe runs once per querier instance

WHEN one querier instance serves multiple queries in a session
THEN the capability probe executes at most once and its verdict is
reused, and because the querier is rebuilt each reconcile pass a
transient scontrol failure denies only that pass — the next pass probes
again
