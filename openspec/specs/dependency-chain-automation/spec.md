# dependency-chain-automation Specification

## Purpose
TBD - created by archiving change m3-slurm-nationalization. Update Purpose after archive.
## Requirements
### Requirement: Five-stage lazy submit orchestration

The orchestrator SHALL submit stages one at a time, polling sacct for completion before deciding whether to submit the next stage. Stages are NOT submitted upfront.

#### Scenario: All five stages are submitted lazily in sequence

- **WHEN** the orchestrator triggers a forecast cycle
- **THEN** stage 1 (`convert_canonical`) MUST be submitted first, using raw source data already produced by node-27 and staged into the compute-visible object store
- **THEN** the orchestrator MUST poll sacct until stage 1 reaches a terminal state
- **THEN** if stage 1 succeeds, stage 2 (`produce_forcing_array`) MUST be submitted
- **THEN** this pattern MUST repeat for all 5 stages in order: `convert_canonical` → `produce_forcing_array` → `run_shud_forecast_array` → `parse_output_array` → `publish_tiles`
- **THEN** each stage's `pipeline_job` record MUST be created at the time of submission, not upfront

#### Scenario: Orchestrator polls sacct for stage completion

- **WHEN** stage N has been submitted and is running
- **THEN** the orchestrator MUST poll sacct at a configurable interval (default 30s) to check the job status
- **THEN** for array job stages, the orchestrator MUST aggregate all task results to determine overall stage outcome
- **THEN** the orchestrator MUST evaluate: all tasks succeeded → submit next stage normally; partial success → submit next stage with reduced basin manifest; all tasks failed → do not submit next stage

#### Scenario: Array job completion triggers aggregation before next submit

- **WHEN** stage 3 (`produce_forcing_array`) is submitted as a job array and receives master job_id `12345`
- **THEN** the orchestrator MUST poll sacct for job `12345` until all array tasks reach terminal states
- **THEN** the orchestrator MUST aggregate task-level results (succeeded/failed/cancelled counts)
- **THEN** only after aggregation MUST the orchestrator decide whether to submit stage 4

---

### Requirement: Cycle-level orchestration

Each orchestration instance SHALL manage one (source, cycle_time) combination. The orchestrator SHALL support simultaneous orchestration of multiple cycles.

#### Scenario: One orchestration per (source, cycle_time)

- **WHEN** the orchestrator receives a trigger for `(source="GFS", cycle_time="2026050700")`
- **THEN** it MUST create one orchestration context for that specific (source, cycle_time) pair
- **THEN** all 6 stages MUST be associated with this single orchestration context
- **THEN** the context MUST include: `source`, `cycle_time`, `basins` (list), and `current_stage` (the last completed or active stage)

#### Scenario: Multiple cycles orchestrated simultaneously

- **WHEN** triggers arrive for `(GFS, 2026050700)` and `(GFS, 2026050706)` within the same time window
- **THEN** each MUST be orchestrated independently with separate stage submissions
- **THEN** jobs from different cycles MUST NOT share dependency chains
- **THEN** each cycle's stages MUST be tracked in separate `ops.pipeline_job` records

#### Scenario: Duplicate orchestration for same (source, cycle_time) is rejected

- **WHEN** an orchestration is already active (non-terminal) for `(GFS, 2026050700)`
- **THEN** a new trigger for the same (source, cycle_time) MUST be rejected
- **THEN** the rejection MUST return an error identifying the existing active orchestration

---

### Requirement: Stage tracking in pipeline_job table

Each stage's pipeline_job record SHALL be created at the time of submission (lazy), not upfront. This enables monitoring and crash recovery.

#### Scenario: pipeline_job record is created when a stage is submitted

- **WHEN** the orchestrator submits stage N to Slurm
- **THEN** a new `ops.pipeline_job` record MUST be created for that stage at submission time
- **THEN** each record MUST include: `slurm_job_id`, `job_type` (one of the 6 upstream stage names), `status`
- **THEN** `submitted_at` MUST be set to the current UTC timestamp

#### Scenario: Stage status is updated from sacct polling

- **WHEN** the status poller queries sacct for all active job_ids in a cycle
- **THEN** each `ops.pipeline_job` record MUST be updated with the current Slurm state
- **THEN** `started_at` MUST be set when status transitions to `RUNNING`
- **THEN** `finished_at` MUST be set when status reaches a terminal state

#### Scenario: Pipeline event is emitted for each stage transition

- **WHEN** a stage's status changes (e.g., `pending` → `running`)
- **THEN** an `ops.pipeline_event` record MUST be inserted with `entity_type='pipeline_job'`, `entity_id`, `event_type='status_change'`, `status_from`, `status_to`

---

### Requirement: Stage failure handling

If any stage fails entirely (all tasks fail), the orchestrator SHALL NOT submit subsequent stages. The orchestrator detects failure via sacct polling and updates the cycle status accordingly.

#### Scenario: Stage failure prevents downstream submission

- **WHEN** stage 3 (`produce_forcing_array`) transitions to `FAILED` (all tasks failed)
- **THEN** the orchestrator MUST NOT submit stages 4-7
- **THEN** no `pipeline_job` records SHALL be created for stages 4-7 (since they are never submitted)
- **THEN** the orchestrator MUST update the `met.forecast_cycle` status to the corresponding `failed_*` state (e.g., `failed_forcing`)

#### Scenario: Failure state mapping per stage

- **WHEN** a stage fails, the cycle status MUST be set according to:
  - stage 1 (`convert_canonical`) fails → `failed_convert`
  - stage 2 (`produce_forcing_array`) fails → `failed_forcing`
  - stage 3 (`run_shud_forecast_array`) fails → `failed_run`
  - stage 4 (`parse_output_array`) fails → `failed_parse`
  - stage 5 (`publish_tiles`) fails → `failed_publish`

#### Scenario: Failure event includes diagnostic detail

- **WHEN** a stage transitions to `FAILED`
- **THEN** the `ops.pipeline_event` record MUST include the sacct exit code and Slurm state string in `message`
- **THEN** if available, stderr from `fetch_logs` MUST be attached to the event

---

### Requirement: Crash recovery from persisted state

The orchestrator SHALL persist its orchestration state such that after a crash or restart, it can resume from the next unsubmitted stage without re-submitting completed stages.

#### Scenario: Orchestrator restarts and resumes from next unsubmitted stage

- **WHEN** the orchestrator process crashes after stage 4 (`run_shud_forecast_array`) has completed successfully
- **THEN** on restart, it MUST query `ops.pipeline_job` for the cycle's completed stages
- **THEN** it MUST identify that the last completed stage is stage 4
- **THEN** it MUST resume by submitting stage 5 (`parse_output_array`), continuing the lazy submit sequence

#### Scenario: Orchestrator restarts while a stage is still running

- **WHEN** the orchestrator crashes while stage 3 is still `running` (has a `slurm_job_id` but no terminal status)
- **THEN** on restart, it MUST resume sacct polling for the in-flight stage 3
- **THEN** it MUST NOT re-submit stage 3
- **THEN** once stage 3 reaches a terminal state, it MUST proceed with the lazy submit logic for stage 4

#### Scenario: All stages already terminal on restart

- **WHEN** the orchestrator restarts and finds all submitted stages in terminal states
- **THEN** it MUST NOT submit any new jobs
- **THEN** it MUST evaluate the final cycle status based on stage outcomes

#### Scenario: No stages submitted yet on restart

- **WHEN** the orchestrator restarts and finds no `pipeline_job` records for a cycle that was triggered but never started
- **THEN** it MUST begin the lazy submit sequence from stage 1 (`convert_canonical`)

---

### Requirement: Concurrency across cycles

Multiple cycles SHALL be orchestrated simultaneously without interference.

#### Scenario: GFS 00Z and 06Z cycles run in parallel

- **WHEN** the orchestrator is managing cycles for `(GFS, 2026050700)` and `(GFS, 2026050706)`
- **THEN** each cycle MUST have its own independent lazy submit sequence
- **THEN** sacct polling MUST query both cycles' active job_ids
- **THEN** a failure in one cycle MUST NOT affect the other cycle's stages

#### Scenario: Different sources run independently

- **WHEN** cycles for `(GFS, 2026050700)` and `(ERA5, 2026-05-06)` are both active
- **THEN** each MUST be orchestrated with independent submission sequences
- **THEN** stage tracking MUST distinguish cycles by `source` and `cycle_time`

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
