## ADDED Requirements

### Requirement: A mixed-cohort forced-resubmit veto SHALL emit one bounded typed receipt without changing eligibility

For a terminal stage with active basins, the orchestrator SHALL preserve the existing forced-resubmit verdict: every basin must satisfy the current closed decision whitelist and canonical restart-stage ordering, and no marker, capability, or exception outside current `master` SHALL qualify a basin. When at least one basin satisfies that predicate and at least one basin does not, the orchestrator SHALL return `False` and capture only the first non-qualifying basin in stable cohort order as one invocation-local `terminal_stage_forced_resubmit_veto` record. The fixed-shape record SHALL contain schema and reason tokens, cycle/run/terminal-job-stage identity, cohort size, qualifying forced-resubmit request count, veto candidate/model/basin identity, the veto decision, canonical restart stage, and a stable veto cause; it SHALL contain no basin list, raw state-evidence mapping, path, URI, secret, or journal payload. The record SHALL attach only to the vetoing candidate's returned `candidate_outcome`, SHALL remain visible in scheduler candidate execution evidence and its bounded candidate summary, and SHALL never be written to the journal. One orchestration invocation SHALL retain at most one such record even when later stage checks or additional basins also veto. Cohorts in which every basin qualifies SHALL still return `True` with no veto record; cohorts in which no basin qualifies SHALL return `False` with no misleading mixed-cohort incident.

#### Scenario: One non-whitelisted basin vetoes a requested cohort replacement visibly

- **WHEN** a terminal forecast job is evaluated for a cohort containing at least one basin whose decision and restart stage qualify for forced resubmission and a later basin whose decision is not in the current whitelist
- **THEN** the gate returns `False` exactly as before, and one typed record names the first veto basin/candidate/model and decision, reports the canonical restart stage, cohort size, and qualifying request count, and is attached only to that candidate outcome

#### Scenario: Multiple vetoes remain bounded to the first stable record

- **WHEN** two or more basins fail the predicate or the gate is evaluated again for a later terminal stage in the same orchestration invocation
- **THEN** the first record remains unchanged and no second veto record or unbounded list is produced

#### Scenario: Uniform cohorts do not manufacture incidents

- **WHEN** every basin qualifies for forced resubmission, or no basin qualifies
- **THEN** the existing boolean verdict is respectively `True` or `False`, and no mixed-cohort veto record is emitted

#### Scenario: Restart-stage ordering veto is typed without changing the order rule

- **WHEN** one basin has a whitelisted decision but its canonical restart stage is absent or later than the terminal job stage while another basin qualifies
- **THEN** the gate remains `False`, and the single record reports the existing stage-order veto cause and the canonical restart-stage value without admitting the basin

#### Scenario: Archived replay marker does not create master eligibility

- **WHEN** a basin carries a `replay_manual_retry_admission`-shaped marker but its decision is not in the current master whitelist
- **THEN** it remains non-qualifying, because that marker contract existed only on a never-merged archived branch, and the mixed cohort remains vetoed under current master semantics

#### Scenario: Scheduler receipt and bounded summary retain the veto

- **WHEN** the chain result is projected into production scheduler candidate evidence and the pass artifact later summarizes candidate rows to honor its byte limit
- **THEN** the vetoing candidate's fixed-shape record remains traceable with the same schema/reason, identities, counts, decision, restart stage, and cause while sibling candidate rows carry no copy

#### Scenario: Observability does not mutate journal authority

- **WHEN** any mixed-cohort veto record is produced
- **THEN** the file journal bytes and decision evidence are unchanged, and the scheduler submits/resumes exactly what the pre-change boolean verdict required
