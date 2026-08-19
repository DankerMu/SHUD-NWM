## ADDED Requirements

### Requirement: Cohort terminal projection receives the master Slurm identity explicitly from its caller

The forecast-cohort terminal projection SHALL receive the master Slurm
identity as an explicit caller-supplied argument — the submit/poll leg
supplies the gateway job's Slurm id and the resume leg supplies the
pipeline row's recorded `slurm_job_id` — never by sniffing an id off
whatever dictionary shape the caller happens to hold, because the resume
leg's terminal dictionary is a pipeline row whose `job_id` is the
pipeline job id, and sniffing it fed the identity-mismatch guard a value
that can never equal the stored Slurm id: every already-terminal
resume-path projection was silently skipped (an idempotent no-op) or
mis-recorded as an identity-pollution event (the non-terminal resume
sub-path polls first and already produced the correct value — the poll
echoes the requested id back, so the sniffed value equalled the bound id;
the fix unifies that sub-path onto the same explicit bound argument with
identical results). An empty or non-numeric
supplied identity on the projection path SHALL fail closed with a
distinct, attributable error instead of degrading into an
unattributable failure. The identity-mismatch guard itself is
unchanged — it was correct and was being fed the wrong value.

#### Scenario: resume reconciles with the real Slurm identity

WHEN an already-terminal forecast-cohort pipeline row that still owes
its projection is resumed
THEN the projection receives the row's recorded Slurm id (not the
pipeline job id) and reconciles as matched-bound against the stored
identity — no identity-mismatch defer, no pollution event

#### Scenario: a settled master is never re-projected

WHEN a resumed forecast-cohort master is already terminal and its
recorded projections fully cover the cohort with terminal outcomes
THEN the resume pass does not re-enter the projection at all: the
durable row's outcome prevails over whatever this pass re-aggregates,
and the row's semantic fields stay byte-identical even when the fresh
aggregation disagrees with the stored one — and when the settled row has
no published log, the pass still publishes the log file to its
deterministic location (the row's log pointer stays unset until a typed
write for it exists)

#### Scenario: the submit leg is unchanged

WHEN the submit/poll leg records a cohort terminal outcome
THEN the projection receives the same gateway Slurm id as before the
change

#### Scenario: a missing identity fails loudly

WHEN the projection path is entered with an empty or non-numeric master
Slurm identity
THEN the call raises a distinct attributable error naming the pipeline
job and stage, rather than degrading into an unattributable downstream
failure
