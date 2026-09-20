## MODIFIED Requirements

### Requirement: Ops strict run identity

The `/ops` display SHALL bind jobs, stages, and logs to the same run identity used by latest-product in cross-plane E2E, and the four backend Ops routes SHALL carry that resolved identity in one coherent top-level envelope object on success, as a sibling of `data`, not inside a stage/job row.

#### Scenario: Backend strict ops identity contract

- **WHEN** an API consumer requests pipeline status, stages, jobs, or job logs with strict identity context
- **THEN** the backend returns a top-level `identity` object containing the resolved `source`, `cycle_time`, `run_id`, and `model_id` on success
- **AND** `job_logs` also includes the exact resolved `job_id`
- **AND** the existing `data` container shape is unchanged
- **AND** partial strict identity fails before source/cycle-only evidence lookup
- **AND** unresolved strict run identity returns `PIPELINE_STRICT_IDENTITY_NOT_FOUND` rather than source/cycle-only evidence


#### Scenario: Ops strict filters

- **WHEN** `/ops` has strict identity context with `source`, `cycle_time`, `run_id`, and `model_id`
- **THEN** pipeline status, stages, jobs, diagnostics, and log requests use or validate that identity
- **AND** jobs from another run with the same source and cycle are rejected or rendered as mismatched
- **AND** the returned `data` payload retains its existing shape while the response envelope carries the coherent identity

#### Scenario: Duplicate source cycle runs

- **WHEN** two runs share the same `source` and `cycle_time`
- **THEN** backend and `/ops` cross-plane evidence passes only for jobs and logs matching the selected `run_id` and `model_id`
- **AND** mixed-run evidence marks cross-plane E2E as fail or blocked

#### Scenario: Strict log identity mismatch

- **WHEN** a strict log request names `source`, `cycle_time`, `run_id`, and `model_id` that do not match the requested `job_id`
- **THEN** the backend returns `PIPELINE_STRICT_IDENTITY_MISMATCH`
- **AND** it does not read or return the wrong job's published log content
