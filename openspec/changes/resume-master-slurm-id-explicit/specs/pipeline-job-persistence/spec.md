## ADDED Requirements

### Requirement: Cohort terminal projection receives the master Slurm identity explicitly from its caller

The forecast-cohort terminal projection SHALL receive the master Slurm
identity as an explicit caller-supplied argument — the submit/poll leg
supplies the gateway job's Slurm id and the resume leg supplies the
pipeline row's recorded `slurm_job_id` — never by sniffing an id off
whatever dictionary shape the caller happens to hold, because the resume
leg's terminal dictionary is a pipeline row whose `job_id` is the
pipeline job id, and sniffing it fed the identity-mismatch guard a value
that can never equal the stored Slurm id: every resume-path projection
was silently skipped (an idempotent no-op against an already-terminal
row) or mis-recorded as an identity-pollution event. An empty supplied
identity on the projection path SHALL fail closed with a distinct,
attributable error instead of degrading into a guaranteed mismatch. The
identity-mismatch guard itself is unchanged — it was correct and was
being fed the wrong value.

#### Scenario: resume re-projects with the real Slurm identity

WHEN an already-terminal forecast-cohort pipeline row is resumed
THEN the projection receives the row's recorded Slurm id (not the
pipeline job id) and performs a real re-projection instead of deferring
with an identity mismatch or returning an idempotent zero-total no-op

#### Scenario: the submit leg is unchanged

WHEN the submit/poll leg records a cohort terminal outcome
THEN the projection receives the same gateway Slurm id as before the
change

#### Scenario: a missing identity fails loudly

WHEN the projection path is entered with an empty master Slurm identity
THEN the call raises a distinct attributable error naming the pipeline
job and stage, rather than proceeding into a guaranteed identity
mismatch
